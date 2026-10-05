"""Fila de análise de mensagens, com fallback síncrono para desenvolvimento."""
from __future__ import annotations

import json
from functools import wraps
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from .models import ActivityLog, FraudDetectionLog, WhatsAppConnection
from .services.ai_service import analyze_message

try:
    from celery import shared_task
except ImportError:  # Celery é opcional no modo local/teste.
    def shared_task(*decorator_args, **decorator_kwargs):
        def decorator(function):
            if decorator_kwargs.get("bind"):
                @wraps(function)
                def local_task(*args, **kwargs):
                    return function(None, *args, **kwargs)
                return local_task
            return function
        return decorator


def _activity(connection, event, message, level=ActivityLog.Level.INFO, **metadata):
    return ActivityLog.objects.create(
        connection=connection,
        event=event,
        message=message[:500],
        level=level,
        metadata=metadata,
    )


def _write_alert_command(company, client, log):
    if not client.phone_number:
        raise ValueError("Número do cliente não configurado")
    command = {
        "log_id": log.id,
        "recipient": f"{client.phone_number}@c.us",
        "message": f"Alerta de segurança: identificamos indícios de golpe. Nível de risco: {log.risk_level}. Não faça PIX nem envie códigos.",
    }
    service_url = f"http://127.0.0.1:{3000 + company.id}/send-alert"
    request = Request(
        service_url,
        data=json.dumps(command).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            if response.status >= 300:
                raise RuntimeError(f"Serviço WhatsApp respondeu HTTP {response.status}")
    except (HTTPError, URLError, TimeoutError) as error:
        details = ""
        if isinstance(error, HTTPError):
            details = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Guardião indisponível: {error}; detalhes: {details}") from error


@shared_task(bind=True, autoretry_for=(), name="antigolpe.analyze_incoming_message")
def analyze_incoming_message(self, connection_id, source_phone, message_id, text):
    """Analisa uma mensagem sem deixar falha de IA interromper o worker."""
    connection = WhatsAppConnection.objects.get(pk=connection_id)
    _activity(connection, "analysis_started", "Analisando mensagem com IA...")
    try:
        analysis = analyze_message(text)
    except Exception as error:  # A fila deve continuar mesmo com provedor de IA indisponível.
        _activity(connection, "analysis_error", "Falha temporária ao analisar a mensagem.", ActivityLog.Level.ERROR, error=str(error))
        return None

    log = FraudDetectionLog.objects.create(
        client_connection=connection,
        source_phone=source_phone[:40],
        message_id=message_id[:180],
        message_text=text[:10000],
        is_fraud=analysis.is_fraud,
        risk_score=analysis.risk_score,
        risk_level=analysis.risk_level,
        reasons=analysis.reasons,
    )
    if not analysis.is_fraud:
        _activity(connection, "analysis_finished", "Mensagem analisada: nenhum indício relevante.", metadata={"score": analysis.risk_score})
        return log.id

    _activity(connection, "fraud_detected", f"Golpe detectado (confiança: {analysis.risk_score}%).", ActivityLog.Level.WARNING, score=analysis.risk_score, reasons=analysis.reasons)
    company = WhatsAppConnection.objects.filter(
        kind=WhatsAppConnection.Kind.COMPANY,
        status=WhatsAppConnection.Status.CONNECTED,
    ).first()
    try:
        if not company:
            raise RuntimeError("Sessão Guardião desconectada")
        _write_alert_command(company, connection, log)
        _activity(connection, "alert_queued", "Alerta enfileirado para o Guardião.")
    except Exception as error:
        log.alert_error = str(error)[:2000]
        log.save(update_fields=["alert_error"])
        _activity(connection, "alert_error", "Não foi possível enfileirar o alerta.", ActivityLog.Level.ERROR, error=str(error))
    return log.id


def enqueue_analysis(connection_id, source_phone, message_id, text):
    """Usa Celery quando habilitado; em desenvolvimento executa sem perder a mensagem."""
    if getattr(settings, "FRAUD_USE_CELERY", False):
        try:
            return analyze_incoming_message.delay(connection_id, source_phone, message_id, text)
        except Exception:
            pass
    return analyze_incoming_message(connection_id, source_phone, message_id, text)