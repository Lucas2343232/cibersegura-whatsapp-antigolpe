"""Motor anti-golpe determinístico, sem dependência externa.

O módulo pode ser trocado por um provedor LLM posteriormente sem alterar as views.
A saída sempre é estruturada como booleano + score + nível + razões.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class FraudAnalysis:
    is_fraud: bool
    risk_score: int
    risk_level: str
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.lower()).strip()


RULES = [
    ("urgencia_financeira", 30, [
        r"pix.*(agora|urgente|urgencia|imediatamente|em \d+ min)",
        r"preciso.*pix", r"faz.*pix", r"manda.*pix", r"pague.*agora",
        r"transferencia.*urgente", r"deposit(a|e).*urgente",
    ]),
    ("pedido_de_codigo", 35, [
        r"codigo.*whatsapp", r"codigo de verificacao", r"codigo.*sms",
        r"me passa.*codigo", r"envia.*codigo", r"numero que chegou",
    ]),
    ("tomada_de_conta", 40, [
        r"sua conta.*bloquead", r"conta.*invadid", r"hacke", r"roubaram.*conta",
        r"validar.*conta", r"confirmar.*conta", r"desbloquear.*conta",
        r"senha.*urgente", r"token.*urgente",
    ]),
    ("falsificacao_de_parente", 35, [
        r"sou eu.*(troquei|perdi|quebrou).*celular", r"meu numero mudou",
        r"celular novo", r"estou sem acesso", r"aqui e seu (filho|filha|mae|pai|irmao|irma)",
        r"sou seu (filho|filha|mae|pai|irmao|irma)",
    ]),
    ("link_suspeito", 30, [
        r"https?://", r"www\.", r"bit\.ly/", r"tinyurl\.com/", r"qr code",
        r"clique aqui", r"acesse este link", r"link de confirmacao",
    ]),
    ("vantagem_exagerada", 20, [
        r"premio", r"ganhou", r"beneficio exclusivo", r"reembolso", r"cashback",
        r"sorteio", r"brinde", r"dinheiro liberado",
    ]),
    ("pressao_social", 15, [
        r"nao conte para ninguem", r"nao avise", r"tem que ser agora", r"nao pode ligar",
        r"confidencial", r"nao fala com",
    ]),
]


def analyze_message(text: str) -> FraudAnalysis:
    """Analisa semântica baseada em padrões de engenharia social.

    Retorna is_fraud=True a partir de 65 pontos. Combina sinais para reduzir
    falsos positivos em mensagens que só mencionam um termo isolado.
    """
    normalized = _normalize(text)
    if not normalized:
        return FraudAnalysis(False, 0, "LOW", [])

    score = 0
    reasons: list[str] = []
    for reason, weight, patterns in RULES:
        if any(re.search(pattern, normalized, re.I) for pattern in patterns):
            score += weight
            reasons.append(reason)

    # Composição típica de fraude: pedido financeiro + urgência; ou link + credencial.
    if "urgencia_financeira" in reasons and "pressao_social" in reasons:
        score += 15
    if "link_suspeito" in reasons and "pedido_de_codigo" in reasons:
        score += 20
    if "link_suspeito" in reasons and "tomada_de_conta" in reasons:
        score += 15
    if "falsificacao_de_parente" in reasons and "urgencia_financeira" in reasons:
        score += 20

    score = min(score, 100)
    if score >= 85:
        level = "CRITICAL"
    elif score >= 65:
        level = "HIGH"
    elif score >= 40:
        level = "MEDIUM"
    else:
        level = "LOW"

    return FraudAnalysis(score >= 65, score, level, reasons)
