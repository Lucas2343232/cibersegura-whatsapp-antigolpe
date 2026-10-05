from django.conf import settings
from django.db import models
from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Usuário da plataforma; staff/superuser acessa o painel da empresa."""

    class Meta:
        db_table = "antigolpe_user"


class CustomerProfile(models.Model):
    """Dados comerciais e do WhatsApp protegido de um cliente SaaS."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="customer_profile")
    full_name = models.CharField(max_length=180)
    ddd = models.CharField(max_length=3)
    phone_number = models.CharField(max_length=15)
    plan_name = models.CharField(max_length=80, default="Proteção Essencial")
    purchase_status = models.CharField(max_length=20, default="APPROVED")
    purchased_at = models.DateTimeField(auto_now_add=True)

    @property
    def protected_phone(self):
        return f"{self.ddd}{self.phone_number}"

    def __str__(self):
        return self.full_name


class WhatsAppConnection(models.Model):
    class Kind(models.TextChoices):
        CLIENT = "CLIENT", "Cliente"
        COMPANY = "COMPANY", "Empresa"

    class Status(models.TextChoices):
        OFFLINE = "OFFLINE", "Offline"
        STARTING = "STARTING", "Iniciando"
        QR_READY = "QR_READY", "Aguardando QR"
        CONNECTED = "CONNECTED", "Conectado"
        DISCONNECTED = "DISCONNECTED", "Desconectado"
        INVALID_SESSION = "INVALID_SESSION", "Sessão inválida"
        ERROR = "ERROR", "Erro"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="whatsapp_connections",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    label = models.CharField(max_length=120)
    phone_number = models.CharField(max_length=30, blank=True)
    session_key = models.SlugField(max_length=160, unique=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OFFLINE)
    qr_code_base64 = models.TextField(blank=True)
    last_error = models.TextField(blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_connected_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    qr_generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "kind"],
                condition=models.Q(kind="CLIENT"),
                name="one_client_connection_per_owner",
            ),
            models.UniqueConstraint(
                fields=["kind"],
                condition=models.Q(kind="COMPANY"),
                name="one_company_connection",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} - {self.label}"


class WhatsAppSession(models.Model):
    """Identidade SaaS consumida pelo Node multi-sessão."""

    class SessionType(models.TextChoices):
        CLIENT = "client", "Cliente / Monitor"
        GUARDIAN = "guardian", "Guardião / Master"

    connection = models.OneToOneField(WhatsAppConnection, on_delete=models.CASCADE, related_name="saas_session")
    customer = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, null=True, blank=True, related_name="whatsapp_sessions")
    client_id = models.SlugField(max_length=180, unique=True)
    session_type = models.CharField(max_length=10, choices=SessionType.choices)
    node_port = models.PositiveIntegerField(default=3001)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.client_id} ({self.get_session_type_display()})"


class FraudDetectionLog(models.Model):
    class RiskLevel(models.TextChoices):
        LOW = "LOW", "Baixo"
        MEDIUM = "MEDIUM", "Médio"
        HIGH = "HIGH", "Alto"
        CRITICAL = "CRITICAL", "Crítico"

    client_connection = models.ForeignKey(
        WhatsAppConnection,
        on_delete=models.CASCADE,
        related_name="fraud_logs",
    )
    source_phone = models.CharField(max_length=40, blank=True)
    message_id = models.CharField(max_length=180, blank=True)
    message_text = models.TextField()
    is_fraud = models.BooleanField(default=False)
    risk_score = models.PositiveSmallIntegerField(default=0)
    risk_level = models.CharField(max_length=12, choices=RiskLevel.choices, default=RiskLevel.LOW)
    reasons = models.JSONField(default=list, blank=True)
    alert_sent = models.BooleanField(default=False)
    alert_error = models.TextField(blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-detected_at"]

    def __str__(self):
        return f"{self.client_connection} / {self.risk_level} / {self.detected_at:%Y-%m-%d %H:%M}"


class ActivityLog(models.Model):
    class Level(models.TextChoices):
        INFO = "INFO", "Informação"
        SUCCESS = "SUCCESS", "Sucesso"
        WARNING = "WARNING", "Atenção"
        ERROR = "ERROR", "Erro"

    connection = models.ForeignKey(
        WhatsAppConnection,
        on_delete=models.CASCADE,
        related_name="activity_logs",
    )
    event = models.CharField(max_length=60)
    message = models.CharField(max_length=500)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.connection} / {self.event} / {self.created_at:%Y-%m-%d %H:%M}"
