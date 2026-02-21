from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from dojo.models import Product_Type_Member, Role

from aist.models import Organization


@dataclass(frozen=True, slots=True)
class DemoUserSpec:
    username: str
    first_name: str
    last_name: str
    email: str
    role_name: str


ORG_NAMES = [
    "Acme Platform",
    "Nova Payments",
    "Helios Core",
]

DEMO_USERS = [
    DemoUserSpec("org_reader", "Org", "Reader", "org_reader@example.local", "Reader"),
    DemoUserSpec("org_writer", "Org", "Writer", "org_writer@example.local", "Writer"),
    DemoUserSpec("org_maintainer", "Org", "Maintainer", "org_maintainer@example.local", "Maintainer"),
    DemoUserSpec("org_owner", "Org", "Owner", "org_owner@example.local", "Owner"),
]


class Command(BaseCommand):
    help = (
        "Bootstrap demo admin username, organizations and users. "
        "Creates organizations/users and binds organization roles via ProductType membership."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default="ChangeMe123!",
            help="Password set for created demo users (and optionally for superuser).",
        )
        parser.add_argument(
            "--skip-admin",
            action="store_true",
            default=False,
            help="Do not modify superuser username/password.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        password = options["password"]
        skip_admin = options["skip_admin"]

        user_model = get_user_model()

        if not skip_admin:
            self._ensure_admin_username(user_model, password=password)

        users_by_role = self._ensure_demo_users(user_model, password=password)
        organizations = self._ensure_organizations()

        self._bind_roles_via_product_type(organizations=organizations, users_by_role=users_by_role)

        self.stdout.write(f"Demo users password: {password}")
        self.stdout.write(f"Organizations: {', '.join(org.name for org in organizations)}")

    def _ensure_admin_username(self, user_model, *, password: str) -> None:
        admin = user_model.objects.filter(is_superuser=True).order_by("id").first()
        if admin is None:
            self.stdout.write("Superuser not found, skip admin rename.")
            return

        if admin.username != "admin":
            existing_admin = user_model.objects.exclude(pk=admin.pk).filter(username="admin").first()
            if existing_admin is not None:
                suffix = 1
                alias = "admin_legacy"
                while user_model.objects.filter(username=alias).exists():
                    suffix += 1
                    alias = f"admin_legacy_{suffix}"
                existing_admin.username = alias
                existing_admin.save(update_fields=["username"])
            admin.username = "admin"
            admin.save(update_fields=["username"])

        if not admin.check_password(password):
            admin.set_password(password)
            admin.save(update_fields=["password"])

    def _ensure_demo_users(self, user_model, *, password: str) -> dict[str, object]:
        users_by_role: dict[str, object] = {}
        for spec in DEMO_USERS:
            user, _ = user_model.objects.get_or_create(
                username=spec.username,
                defaults={
                    "first_name": spec.first_name,
                    "last_name": spec.last_name,
                    "email": spec.email,
                    "is_active": True,
                },
            )
            updates: list[str] = []
            if user.first_name != spec.first_name:
                user.first_name = spec.first_name
                updates.append("first_name")
            if user.last_name != spec.last_name:
                user.last_name = spec.last_name
                updates.append("last_name")
            if user.email != spec.email:
                user.email = spec.email
                updates.append("email")
            if updates:
                user.save(update_fields=updates)

            if not user.check_password(password):
                user.set_password(password)
                user.save(update_fields=["password"])
            users_by_role[spec.role_name] = user
        return users_by_role

    def _ensure_organizations(self) -> list[Organization]:
        organizations: list[Organization] = []
        for idx, org_name in enumerate(ORG_NAMES, start=1):
            org, _ = Organization.objects.get_or_create(
                name=org_name,
                defaults={"description": f"Demo organization {idx}"},
            )
            organizations.append(org)
        return organizations

    def _bind_roles_via_product_type(self, *, organizations: list[Organization], users_by_role: dict[str, object]) -> None:
        role_names = {spec.role_name for spec in DEMO_USERS}
        roles = {role.name: role for role in Role.objects.filter(name__in=role_names)}
        missing_roles = sorted(role_names - set(roles))
        if missing_roles:
            msg = f"Missing roles in DB: {', '.join(missing_roles)}"
            raise CommandError(msg)

        for org in organizations:
            product_type = org.ensure_product_type()
            for spec in DEMO_USERS:
                Product_Type_Member.objects.get_or_create(
                    product_type=product_type,
                    user=users_by_role[spec.role_name],
                    defaults={"role": roles[spec.role_name]},
                )
