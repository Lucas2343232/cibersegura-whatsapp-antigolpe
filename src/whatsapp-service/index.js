const path = require('path');
const fs = require('fs/promises');
const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const QRCode = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');

const PORT = Number(process.env.PORT || 3001);
const RUNTIME_ROOT = process.env.WHATSAPP_RUNTIME_ROOT || path.join(__dirname, '..', 'runtime');
const CALLBACK_URL = process.env.WHATSAPP_BOT_CALLBACK_URL || 'http://127.0.0.1:8000/whatsapp/bot-event/';
const CALLBACK_TOKEN = process.env.WHATSAPP_BOT_CALLBACK_TOKEN || '';
const legacyConnections = {
  monitor: Number(process.env.WHATSAPP_MONITOR_CONNECTION_ID || 0),
  guardian: Number(process.env.WHATSAPP_GUARDIAN_CONNECTION_ID || 0),
};
const sessions = new Map();
const states = new Map();

const app = express();
app.use(express.json({ limit: '64kb' }));
const server = http.createServer(app);
const io = new Server(server, { cors: { origin: process.env.FRONTEND_ORIGIN || '*' } });

function setState(role, patch) {
  const state = { ...(states.get(role) || { role, status: 'OFFLINE' }), ...patch, updatedAt: new Date().toISOString() };
  states.set(role, state);
  io.emit('whatsapp:status', state);
  return state;
}

async function notifyDjango(session, event, payload = {}) {
  const connectionId = session.connectionId;
  if (!connectionId || !CALLBACK_TOKEN) return;
  const response = await fetch(CALLBACK_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-WhatsApp-Bot-Token': CALLBACK_TOKEN },
    body: JSON.stringify({ connection_id: connectionId, event, ...payload }),
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error(`Django respondeu HTTP ${response.status}`);
}

function browserOptions(role) {
  return {
    authStrategy: new LocalAuth({
      clientId: `whatsapp-${role}`,
      dataPath: path.join(RUNTIME_ROOT, 'whatsapp_sessions'),
    }),
    puppeteer: {
      headless: true,
      executablePath: process.env.WHATSAPP_CHROME_PATH || undefined,
      // Evita sandbox incompatível e reduz consumo; não use o Chromium do sistema se ele travar.
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    },
  };
}

function bindEvents(session, client) {
  const { clientId, role } = session;
  client.on('qr', async (qr) => {
    try {
      const qrCodeBase64 = (await QRCode.toDataURL(qr, { width: 320, margin: 2 })).split(',')[1];
      setState(clientId, { status: 'QR_READY', qrCodeBase64, lastError: '' });
      await notifyDjango(session, 'qr', { qr_code_base64: qrCodeBase64 });
    } catch (error) {
      setState(clientId, { status: 'ERROR', lastError: error.message });
    }
  });
  client.on('ready', async () => {
    setState(clientId, { status: 'CONNECTED', qrCodeBase64: '', lastError: '' });
    try { await notifyDjango(session, 'ready'); } catch (error) { setState(clientId, { lastError: error.message }); }
  });
  client.on('auth_failure', async (message) => {
    setState(clientId, { status: 'INVALID_SESSION', lastError: String(message) });
    try { await notifyDjango(session, 'error', { error: String(message), auth_failure: true }); } catch (_) {}
  });
  client.on('disconnected', async (reason) => {
    setState(clientId, { status: 'DISCONNECTED', qrCodeBase64: '', lastError: String(reason) });
    sessions.delete(clientId);
    try { await notifyDjango(session, 'disconnected', { error: String(reason) }); } catch (_) {}
  });
  client.on('message', async (message) => {
    if (role !== 'client' || message.fromMe || !message.from || message.from.endsWith('@g.us') || !message.body.trim()) return;
    try {
      await notifyDjango(session, 'message', {
        message_id: message.id && message.id._serialized,
        source_phone: message.from,
        text: message.body,
      });
    } catch (error) {
      setState(clientId, { lastError: `Webhook: ${error.message}` });
    }
  });
}

async function startSession(clientId, options = {}) {
  if (sessions.has(clientId)) return states.get(clientId);
  const role = options.role || (clientId === 'guardian-master' ? 'guardian' : 'client');
  const session = { clientId, role, connectionId: Number(options.connectionId || 0) };
  const client = new Client({
    ...browserOptions(role),
    authStrategy: new LocalAuth({ clientId: `whatsapp-${clientId}`, dataPath: path.join(RUNTIME_ROOT, 'whatsapp_sessions') }),
  });
  sessions.set(clientId, { ...session, client });
  setState(clientId, { clientId, role, status: 'STARTING', qrCodeBase64: '', lastError: '' });
  bindEvents(session, client);
  try {
    await client.initialize();
  } catch (error) {
    sessions.delete(clientId);
    setState(clientId, { status: 'ERROR', lastError: error.message || String(error) });
    try { await notifyDjango(session, 'error', { error: error.message || String(error) }); } catch (_) {}
    // Destruir o Chromium evita que uma falha do Puppeteer deixe o próximo ciclo congelado.
    try { await client.destroy(); } catch (_) {}
  }
  return states.get(role);
}

async function stopSession(clientId) {
  const session = sessions.get(clientId);
  const client = session?.client;
  sessions.delete(clientId);
  if (client) {
    try { await client.logout(); } catch (_) {}
    try { await client.destroy(); } catch (_) {}
  }
  setState(clientId, { status: 'OFFLINE', qrCodeBase64: '' });
}

app.get('/health', (_req, res) => res.json({ ok: true, sessions: [...states.values()] }));
app.get('/sessions/:clientId', (req, res) => {
  if (!states.has(req.params.clientId)) return res.status(404).json({ error: 'Sessão desconhecida' });
  return res.json(states.get(req.params.clientId));
});
app.post('/sessions/:clientId/start', async (req, res) => {
  const clientId = req.params.clientId;
  const role = req.body?.role || (clientId === 'guardian-master' ? 'guardian' : 'client');
  if (!['client', 'guardian'].includes(role)) return res.status(400).json({ error: 'role inválida' });
  return res.status(202).json(await startSession(clientId, { role, connectionId: req.body?.connection_id }));
});
app.post('/sessions/:clientId/stop', async (req, res) => {
  await stopSession(req.params.clientId);
  return res.json({ ok: true });
});
app.post('/send-alert', async (req, res) => {
  const { recipient, message, log_id: logId } = req.body || {};
  const guardianId = req.body?.guardian_client_id || 'guardian-master';
  const guardian = sessions.get(guardianId);
  const client = guardian?.client;
  if (!client || states.get(guardianId)?.status !== 'CONNECTED') return res.status(503).json({ error: 'Guardião desconectado' });
  if (!recipient || !message) return res.status(400).json({ error: 'recipient e message são obrigatórios' });
  try {
    const normalizedRecipient = recipient.endsWith('@c.us') ? recipient : `${recipient}@c.us`;
    const numberId = await client.getNumberId(normalizedRecipient);
    if (!numberId) return res.status(422).json({ error: `O número ${normalizedRecipient} não possui uma conta WhatsApp válida` });
    await client.sendMessage(numberId._serialized, message);
    await notifyDjango(guardian, 'alert_sent', { log_id: logId });
    return res.json({ ok: true });
  } catch (error) {
    return res.status(502).json({ error: error.message || String(error) });
  }
});

io.on('connection', (socket) => socket.emit('whatsapp:status', [...states.values()]));
server.listen(PORT, async () => {
  console.log(`WhatsApp service ouvindo na porta ${PORT}`);
  if (process.env.WHATSAPP_AUTO_START === '1') {
    const role = process.env.WHATSAPP_ROLE || 'client';
    const clientId = process.env.WHATSAPP_CLIENT_ID || (role === 'guardian' ? 'guardian-master' : `customer-${legacyConnections.monitor}`);
    const connectionId = role === 'guardian' ? legacyConnections.guardian : legacyConnections.monitor;
    await startSession(clientId, { role, connectionId });
  }
});

setInterval(() => {
  for (const [, session] of sessions) notifyDjango(session, 'heartbeat').catch(() => {});
}, 10000).unref();

process.on('SIGTERM', async () => {
  await Promise.all([...sessions.keys()].map(stopSession));
  await fs.rm(path.join(RUNTIME_ROOT, 'whatsapp_node_tmp'), { recursive: true, force: true }).catch(() => {});
  process.exit(0);
});
process.on('unhandledRejection', (error) => console.error('Rejeição não tratada:', error));
