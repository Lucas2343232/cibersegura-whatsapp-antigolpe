# CiberSegura — Recorte de Portfólio

Projeto desenvolvido como Trabalho de Conclusão de Curso (TCC): **Cibersegurança Inclusiva: Assistente Virtual para Identificação de Engenharia Social no WhatsApp**.

> Este repositório é um **recorte técnico para portfólio**. O objetivo é demonstrar arquitetura, modelagem, análise de mensagens, processamento assíncrono e integração com WhatsApp sem expor sessões, banco local, `node_modules`, logs ou credenciais.

## O que o projeto demonstra

- **Python + Django** para autenticação, regras de negócio, endpoints e painel web.
- **Modelagem relacional** com Django ORM para usuários, conexões, sessões, eventos e incidentes.
- **Detecção de engenharia social** com normalização de texto, regras/padrões, pontuação de risco e classificação.
- **Celery + Redis** como opção de processamento assíncrono.
- **Node.js + Express** para manter sessões WhatsApp separadas do processo web Django.
- **whatsapp-web.js** para integração com WhatsApp Web e geração de QR Code.
- Comunicação entre Django e Node por **webhooks HTTP**.
- Painel administrativo com indicadores de clientes, sessões, mensagens e alertas.
- Interface responsiva em Django Templates/HTML/CSS/JavaScript.
- Cuidados de segurança: `.env`, isolamento de sessões, autenticação e proteção CSRF.

## Arquitetura resumida

```text
WhatsApp do cliente
        │
        ▼
  Node.js / whatsapp-web.js
        │  webhook
        ▼
      Django
        │
        ├── Motor de análise de engenharia social
        │       └── score + nível + motivos
        │
        ├── Banco de dados
        │       ├── usuários
        │       ├── sessões
        │       ├── mensagens/incidentes
        │       └── logs
        │
        └── Celery/Redis (opcional)
                │
                ▼
        WhatsApp Guardião
                │
                ▼
          alerta ao cliente
```

## Principais arquivos apresentados

| Arquivo | O que demonstra |
|---|---|
| `src/antigolpe/services/ai_service.py` | Motor de análise e pontuação de risco |
| `src/antigolpe/models.py` | Modelagem das entidades do sistema |
| `src/antigolpe/tasks.py` | Processamento assíncrono e fluxo de alertas |
| `src/antigolpe/views.py` | Regras de negócio e endpoints Django |
| `src/antigolpe/services/whatsapp_worker.py` | Gerenciamento de processos de integração |
| `src/whatsapp-service/index.js` | Microsserviço Node e sessões WhatsApp |
| `templates/antigolpe/*.html` | Interfaces do produto |
| `src/config/celery.py` | Configuração da fila de tarefas |

## Exemplos de engenharia de software

### Análise de risco

As mensagens são normalizadas para reduzir diferenças de acentuação e espaçamento. Em seguida, regras independentes identificam sinais como:

- urgência financeira;
- pedido de código de verificação;
- tentativa de tomada de conta;
- falsificação de identidade;
- links suspeitos;
- vantagem financeira exagerada;
- pressão social.

Os sinais recebem pesos e podem ser combinados para aumentar a pontuação de risco.

### Separação de responsabilidades

O Django concentra a aplicação web e a persistência. A comunicação com o WhatsApp é isolada em um serviço Node.js. Isso reduz o acoplamento e permite que a camada de integração seja substituída futuramente por uma API oficial.

### Processamento assíncrono

O projeto possui suporte a Celery/Redis para que a análise das mensagens não precise bloquear o fluxo principal da aplicação.

## Observação importante

A implementação demonstrada utiliza automação do WhatsApp Web (`whatsapp-web.js`) porque esse foi o escopo do protótipo acadêmico. Para um produto comercial, a arquitetura deve ser adaptada à **API oficial do WhatsApp Business/Meta**, às políticas do serviço e aos requisitos de privacidade e retenção de dados.

## Telas

As imagens em `screenshots/` mostram:

1. Landing page do CiberSegura.
2. Painel administrativo/Guardião.
3. Área de monitoramento do cliente.

## Tecnologias

**Python · Django · Django ORM · Celery · Redis · Node.js · Express · whatsapp-web.js · HTML · CSS · JavaScript · SQLite**

---
