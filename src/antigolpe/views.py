from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.utils import timezone
from django.db.models import Count
from django.db import transaction
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
import secrets
import string
import json

from .models import ActivityLog, CustomerProfile, FraudDetectionLog, WhatsAppConnection, WhatsAppSession
from .tasks import enqueue_analysis
from .services.whatsapp_worker import start_background_worker, stop_background_worker


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is None:
            messages.error(request, "Usuário ou senha inválidos.")
        else:
            login(request, user)
            return redirect("dashboard")
    return render(request, "antigolpe/login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


def _is_staff(user):
    return user.is_authenticated and user.is_staff


def home(request):
    if request.user.is_authenticated:
        return redirect("admin_dashboard" if request.user.is_staff else "dashboard")
    return render(request, "antigolpe/landing.html")


def checkout(request):
    if request.user.is_authenticated:
        return redirect("admin_dashboard" if request.user.is_staff else "dashboard")
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        ddd = "".join(ch for ch in request.POST.get("ddd", "") if ch.isdigit())
        phone_number = "".join(ch for ch in request.POST.get("phone_number", "") if ch.isdigit())
        errors = []
        try:
            validate_email(email)
        except ValidationError:
            errors.append("Informe um e-mail válido.")
        if len(full_name) < 3:
            errors.append("Informe seu nome completo.")
        if len(ddd) not in (2, 3) or not 8 <= len(phone_number) <= 9:
            errors.append("Informe DDD e telefone válidos.")
        if get_user_model().objects.filter(username=email).exists():
            errors.append("Este e-mail já possui uma conta. Acesse o login.")
        if errors:
            return render(request, "antigolpe/checkout.html", {"errors": errors, "form_data": request.POST})

        alphabet = string.ascii_letters + string.digits
        temporary_password = "Ciber-" + "".join(secrets.choice(alphabet) for _ in range(10))
        with transaction.atomic():
            user = get_user_model().objects.create(
                username=email,
                email=email,
                first_name=full_name.split()[0],
                last_name=" ".join(full_name.split()[1:]),
                password=make_password(temporary_password),
            )
            profile = CustomerProfile.objects.create(
                user=user, full_name=full_name, ddd=ddd, phone_number=phone_number,
            )
            connection = WhatsAppConnection.objects.create(
                owner=user, kind=WhatsAppConnection.Kind.CLIENT,
                label=f"Proteção de {full_name}", phone_number=profile.protected_phone,
                session_key=f"cliente-{user.pk}",
            )
            WhatsAppSession.objects.create(
                connection=connection, customer=profile,
                client_id=f"customer-{user.pk}",
                session_type=WhatsAppSession.SessionType.CLIENT,
                node_port=3000 + connection.id,
            )
        request.session["new_credentials"] = {"username": email, "password": temporary_password}
        return redirect("checkout_success")
    return render(request, "antigolpe/checkout.html")


def checkout_success(request):
    credentials = request.session.pop("new_credentials", None)
    if not credentials:
        return redirect("checkout")
    return render(request, "antigolpe/success.html", {"credentials": credentials})


@login_required
def dashboard(request):
    if request.user.is_staff:
        return redirect("admin_dashboard")
    profile = getattr(request.user, "customer_profile", None)
    connection, _ = WhatsAppConnection.objects.get_or_create(
        owner=request.user,
        kind=WhatsAppConnection.Kind.CLIENT,
        defaults={
            "label": f"WhatsApp de {request.user.get_full_name() or request.user.username}",
            "session_key": f"cliente-{request.user.pk}",
        },
    )
    # O start é idempotente. Para ambientes com múltiplos workers, mova o bot para um processo dedicado.
    if profile and not hasattr(connection, "saas_session"):
        WhatsAppSession.objects.create(
            connection=connection, customer=profile, client_id=f"customer-{request.user.pk}",
            session_type=WhatsAppSession.SessionType.CLIENT, node_port=3000 + connection.id,
        )
    return render(request, "antigolpe/client_dashboard.html", {"connection": connection, "profile": profile})


@user_passes_test(_is_staff, login_url="login")
def admin_dashboard(request):
    guardian, _ = WhatsAppConnection.objects.get_or_create(
        kind=WhatsAppConnection.Kind.COMPANY,
        defaults={"label": "Guardião Master", "session_key": "guardian-master"},
    )
    WhatsAppSession.objects.get_or_create(
        connection=guardian, defaults={
            "client_id": "guardian-master", "session_type": WhatsAppSession.SessionType.GUARDIAN,
            "node_port": 3000 + guardian.id,
        },
    )
    metrics = {
        "customers": CustomerProfile.objects.count(),
        "active_customers": WhatsAppConnection.objects.filter(kind=WhatsAppConnection.Kind.CLIENT, status=WhatsAppConnection.Status.CONNECTED).count(),
        "messages": ActivityLog.objects.filter(event="message_received").count(),
        "alerts_sent": FraudDetectionLog.objects.filter(alert_sent=True).count(),
    }
    return render(request, "antigolpe/admin_dashboard.html", {"connection": guardian, "metrics": metrics})


@login_required
@require_GET
def client_status(request):
    connection = get_object_or_404(
        WhatsAppConnection, owner=request.user, kind=WhatsAppConnection.Kind.CLIENT
    )
    # Inicia automaticamente caso o usuário abra diretamente o endpoint.
    connection.refresh_from_db()
    labels = {
        WhatsAppConnection.Status.CONNECTED: "SISTEMA OPERACIONAL / MONITORANDO",
        WhatsAppConnection.Status.QR_READY: "QR CODE DISPONÍVEL / AGUARDANDO LEITURA",
        WhatsAppConnection.Status.DISCONNECTED: "DESCONECTADO / TENTE GERAR NOVO QR",
        WhatsAppConnection.Status.INVALID_SESSION: "SESSÃO INVÁLIDA / LEIA NOVO QR",
        WhatsAppConnection.Status.ERROR: "ERRO NO WORKER / VERIFIQUE O LOG",
    }
    return JsonResponse({
        "status": connection.status,
        "connected": connection.status == WhatsAppConnection.Status.CONNECTED,
        "status_label": labels.get(connection.status, "AGUARDANDO CONEXÃO"),
        "qr_code_base64": connection.qr_code_base64,
        "last_connected_at": connection.last_connected_at.isoformat() if connection.last_connected_at else None,
        "last_error": connection.last_error,
        "last_event_at": connection.last_event_at.isoformat() if connection.last_event_at else None,
    })


@login_required
@require_GET
def client_activity_logs(request):
    connection = get_object_or_404(WhatsAppConnection, owner=request.user, kind=WhatsAppConnection.Kind.CLIENT)
    logs = connection.activity_logs.values("id", "event", "message", "level", "created_at")[:50]
    return JsonResponse({"logs": list(logs)})


@login_required
@require_POST
def client_generate_qr(request):
    connection = get_object_or_404(
        WhatsAppConnection, owner=request.user, kind=WhatsAppConnection.Kind.CLIENT
    )
    started = start_background_worker(connection.id, restart=True)
    if started:
        connection.status = WhatsAppConnection.Status.STARTING
        connection.last_error = ""
        connection.save(update_fields=["status", "last_error", "updated_at"])
    return JsonResponse({
        "ok": True,
        "started": started,
        "message": "Gerando QR Code..." if started else "O gerador de QR ja esta em execucao.",
    })


@login_required
@require_POST
def client_update_phone(request):
    connection = get_object_or_404(
        WhatsAppConnection, owner=request.user, kind=WhatsAppConnection.Kind.CLIENT
    )
    phone = "".join(ch for ch in request.POST.get("phone_number", "") if ch.isdigit())
    if len(phone) < 10 or len(phone) > 15:
        return JsonResponse({"ok": False, "error": "Informe DDI + DDD + número (somente dígitos)."}, status=400)
    connection.phone_number = phone
    connection.save(update_fields=["phone_number", "updated_at"])
    return JsonResponse({"ok": True, "phone_number": phone})


@user_passes_test(_is_staff, login_url="login")
def admin_empresa(request):
    company, _ = WhatsAppConnection.objects.get_or_create(
        kind=WhatsAppConnection.Kind.COMPANY,
        defaults={
            "label": "WhatsApp Oficial de Envio",
            "session_key": "empresa-oficial",
        },
    )
    recent_logs = FraudDetectionLog.objects.select_related("client_connection")[:20]
    totals = FraudDetectionLog.objects.values("risk_level").annotate(total=Count("id"))
    return render(request, "antigolpe/admin_empresa.html", {
        "connection": company,
        "recent_logs": recent_logs,
        "totals": totals,
    })


@user_passes_test(_is_staff, login_url="login")
@require_GET
def company_status(request):
    company = WhatsAppConnection.objects.filter(kind=WhatsAppConnection.Kind.COMPANY).first()
    if not company:
        return JsonResponse({"status": "OFFLINE", "connected": False, "qr_code_base64": ""})
    company.refresh_from_db()
    return JsonResponse({
        "status": company.status,
        "connected": company.status == WhatsAppConnection.Status.CONNECTED,
        "status_label": (
            "Empresa Ativa / Pronta para enviar alertas"
            if company.status == WhatsAppConnection.Status.CONNECTED
            else "WhatsApp Oficial aguardando conexão"
        ),
        "qr_code_base64": company.qr_code_base64,
        "last_error": company.last_error,
    })


@user_passes_test(_is_staff, login_url="login")
@require_POST
def company_generate_qr(request):
    company, _ = WhatsAppConnection.objects.get_or_create(
        kind=WhatsAppConnection.Kind.COMPANY,
        defaults={
            "label": "WhatsApp Oficial de Envio",
            "session_key": "empresa-oficial",
        },
    )
    started = start_background_worker(company.id, restart=True)
    if started:
        company.status = WhatsAppConnection.Status.STARTING
        company.last_error = ""
        company.save(update_fields=["status", "last_error", "updated_at"])
    return JsonResponse({
        "ok": True,
        "started": started,
        "message": "Gerando QR Code do WhatsApp oficial..." if started else "O gerador de QR ja esta em execucao.",
    })


@user_passes_test(_is_staff, login_url="login")
@require_POST
def company_stop_session(request):
    company = get_object_or_404(WhatsAppConnection, kind=WhatsAppConnection.Kind.COMPANY)
    stop_background_worker(company.id)
    company.status = WhatsAppConnection.Status.OFFLINE
    company.qr_code_base64 = ""
    company.save(update_fields=["status", "qr_code_base64", "updated_at"])
    ActivityLog.objects.create(connection=company, event="session_stopped", message="Sessão Guardião derrubada pelo administrador.", level=ActivityLog.Level.WARNING)
    return JsonResponse({"ok": True})


@csrf_exempt
@require_POST
def whatsapp_bot_event(request):
    """Recebe eventos locais do whatsapp-web.js; não é uma rota pública."""
    if request.headers.get("X-WhatsApp-Bot-Token") != settings.WHATSAPP_BOT_CALLBACK_TOKEN:
        return JsonResponse({"error": "unauthorized"}, status=403)
    try:
        payload = json.loads(request.body)
        connection = WhatsAppConnection.objects.get(pk=int(payload["connection_id"]))
    except (ValueError, KeyError, json.JSONDecodeError, WhatsAppConnection.DoesNotExist):
        return JsonResponse({"error": "invalid event"}, status=400)

    event = payload.get("event")
    now = timezone.now()
    connection.last_event_at = now
    if event == "heartbeat":
        from .services.whatsapp_worker import heartbeat_worker
        heartbeat_worker(connection.id)
    elif event == "qr":
        connection.status = WhatsAppConnection.Status.QR_READY
        connection.qr_code_base64 = payload.get("qr_code_base64", "")
        connection.last_error = ""
        connection.qr_generated_at = now
        connection.save(update_fields=["status", "qr_code_base64", "last_error", "qr_generated_at", "last_event_at", "updated_at"])
        ActivityLog.objects.create(connection=connection, event="qr_ready", message="Novo QR Code disponível.")
        return JsonResponse({"ok": True})
    elif event == "ready":
        connection.status = WhatsAppConnection.Status.CONNECTED
        connection.qr_code_base64 = ""
        connection.last_error = ""
        connection.last_connected_at = timezone.now()
        connection.save(update_fields=["status", "qr_code_base64", "last_error", "last_connected_at", "last_event_at", "updated_at"])
        ActivityLog.objects.create(connection=connection, event="connected", message="WhatsApp conectado e monitorando.", level=ActivityLog.Level.SUCCESS)
    elif event == "disconnected":
        connection.status = WhatsAppConnection.Status.DISCONNECTED
        connection.qr_code_base64 = ""
        connection.last_error = payload.get("error", "")[:2000]
        connection.save(update_fields=["status", "qr_code_base64", "last_error", "last_event_at", "updated_at"])
        ActivityLog.objects.create(connection=connection, event="disconnected", message="Sessão WhatsApp desconectada.", level=ActivityLog.Level.WARNING)
    elif event == "error":
        connection.status = WhatsAppConnection.Status.INVALID_SESSION if payload.get("auth_failure") else WhatsAppConnection.Status.ERROR
        connection.last_error = payload.get("error", "Erro no worker WhatsApp")[:2000]
        connection.save(update_fields=["status", "last_error", "last_event_at", "updated_at"])
        ActivityLog.objects.create(connection=connection, event="worker_error", message="Falha no worker WhatsApp.", level=ActivityLog.Level.ERROR, metadata={"error": connection.last_error})
    elif event == "message" and connection.kind == WhatsAppConnection.Kind.CLIENT:
        text = str(payload.get("text", ""))[:10000]
        if text:
            connection.last_message_at = now
            connection.save(update_fields=["last_message_at", "last_event_at", "updated_at"])
            ActivityLog.objects.create(connection=connection, event="message_received", message=f"Mensagem recebida de {payload.get('source_phone', 'contato')}.")
            if not payload.get("message_id") or not FraudDetectionLog.objects.filter(message_id=payload["message_id"]).exists():
                try:
                    enqueue_analysis(connection.id, str(payload.get("source_phone", "")), str(payload.get("message_id", "")), text)
                except Exception as error:
                    ActivityLog.objects.create(connection=connection, event="queue_error", message="Mensagem recebida, mas não foi possível iniciar a análise.", level=ActivityLog.Level.ERROR, metadata={"error": str(error)[:500]})
    elif event == "alert_sent":
        FraudDetectionLog.objects.filter(pk=payload.get("log_id")).update(alert_sent=True, alert_error="")
        ActivityLog.objects.create(connection=connection, event="alert_sent", message="Alerta enviado para a vítima.", level=ActivityLog.Level.SUCCESS)
    else:
        connection.save(update_fields=["last_event_at", "updated_at"])
    return JsonResponse({"ok": True})
