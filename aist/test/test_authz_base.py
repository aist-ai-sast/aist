"""
Tests for AISTAPIView (aist/authz/base.py).

Task 4 — an endpoint without a valid ``authz`` cannot be defined (ImproperlyConfigured
at class-creation time); valid declarations load, and INTERNAL_SERVICE views are
service-principal gated.
Task 5 — ``resolve()`` picks the read vs write permission from the HTTP method and
stays tenant-scoped (cross-org identifier → 404; a Reader cannot resolve a write).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.test import TestCase
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory

from aist.authz import INTERNAL_SERVICE, PUBLIC, Action, AISTAPIView, ResourcePolicy
from aist.authz.permissions import IsInternalService
from aist.models import Organization

User = get_user_model()


# --- module-level views: their mere definition exercises __init_subclass__ ---

class _PublicView(AISTAPIView):
    authz = PUBLIC


class _ServiceView(AISTAPIView):
    authz = INTERNAL_SERVICE


class _ProductView(AISTAPIView):
    # read = Product_View (Reader+); write = Product_Edit (Maintainer+)
    authz = ResourcePolicy(resource=Product, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)


class _AbstractBase(AISTAPIView, abstract=True):

    """Intermediate base — deliberately declares no authz."""


class InitSubclassEnforcementTests(TestCase):

    """Task 4."""

    def test_missing_authz_raises_at_definition(self):
        with self.assertRaises(ImproperlyConfigured):
            class _Bad(AISTAPIView):
                pass

    def test_invalid_authz_value_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            class _Bad2(AISTAPIView):
                authz = "write"

    def test_abstract_base_is_exempt(self):
        # Defining a concrete subclass of the abstract base still requires authz…
        with self.assertRaises(ImproperlyConfigured):
            class _ConcreteChild(_AbstractBase):
                pass
        # …but a further abstract layer is fine.

        class _AbstractChild(_AbstractBase, abstract=True):
            pass

        self.assertTrue(issubclass(_AbstractChild, AISTAPIView))

    def test_internal_service_view_uses_service_permission(self):
        self.assertEqual(_ServiceView.permission_classes, [IsInternalService])

    def test_internal_service_view_cannot_override_permission_classes(self):
        # Declaring a weaker permission_classes under INTERNAL_SERVICE is a loud
        # import-time error, not a silently-honored override.
        with self.assertRaises(ImproperlyConfigured):
            class _LeakyService(AISTAPIView):
                authz = INTERNAL_SERVICE
                permission_classes = [IsAuthenticated]

    def test_public_view_keeps_default_permission(self):
        # PUBLIC does not force IsInternalService; default IsAuthenticated stands
        # unless the view overrides it.
        self.assertNotEqual(_PublicView.permission_classes, [IsInternalService])


class ResolveScopingTests(TestCase):

    """Task 5."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.sla = SLA_Configuration.objects.create(name="SLA")
        self.role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})

        self.pt_a = Product_Type.objects.create(name="Org A")
        self.org_a = Organization.objects.create(name="Org A", product_type=self.pt_a)
        self.prod_a = Product.objects.create(
            name="A1", description="d", prod_type=self.pt_a, sla_configuration_id=self.sla.id,
        )

        self.pt_b = Product_Type.objects.create(name="Org B")
        self.org_b = Organization.objects.create(name="Org B", product_type=self.pt_b)

        self.reader_a = User.objects.create_user("reader_a", "reader_a@example.com", "pass")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.reader_a, role=self.role_reader)

        self.maintainer_a = User.objects.create_user("maint_a", "maint_a@example.com", "pass")
        Product_Type_Member.objects.create(product_type=self.pt_a, user=self.maintainer_a, role=self.role_maintainer)

        self.member_b = User.objects.create_user("member_b", "member_b@example.com", "pass")
        Product_Type_Member.objects.create(product_type=self.pt_b, user=self.member_b, role=self.role_maintainer)

    def _view(self, method: str, user):
        request = getattr(self.factory, method.lower())("/api/v2/aist/x/")
        request.user = user
        view = _ProductView()
        view.request = request
        return view

    def test_reader_can_resolve_read(self):
        view = self._view("GET", self.reader_a)
        self.assertEqual(view.resolve(pk=self.prod_a.id).id, self.prod_a.id)

    def test_reader_cannot_resolve_write(self):
        # POST → write permission (Product_Edit); a Reader is not in that queryset.
        view = self._view("POST", self.reader_a)
        with self.assertRaises(Http404):
            view.resolve(pk=self.prod_a.id)

    def test_maintainer_can_resolve_write(self):
        view = self._view("POST", self.maintainer_a)
        self.assertEqual(view.resolve(pk=self.prod_a.id).id, self.prod_a.id)

    def test_cross_org_read_is_404(self):
        # A member of Org B cannot resolve Org A's product, even for a read.
        view = self._view("GET", self.member_b)
        with self.assertRaises(Http404):
            view.resolve(pk=self.prod_a.id)
