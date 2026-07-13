from django.test import TestCase

from .context import clear_current_tenant, get_current_tenant, tenant_context
from .middleware import TenantMiddleware
from .models import Project, Tenant
from .tokens import make_tenant_token


class TenantScopingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Acme", slug="acme")
        cls.tenant_b = Tenant.objects.create(name="Globex", slug="globex")
        Project.all_objects.create(tenant=cls.tenant_a, name="A-only")
        Project.all_objects.create(tenant=cls.tenant_b, name="B-only")

    def tearDown(self):
        clear_current_tenant()

    def test_all_does_not_bypass_scoping(self):
        with tenant_context(self.tenant_a):
            names = set(Project.objects.all().values_list("name", flat=True))
        self.assertEqual(names, {"A-only"})

    def test_tenant_a_cannot_read_tenant_b_via_filter(self):
        # even explicitly filtering for B's row returns nothing
        with tenant_context(self.tenant_a):
            self.assertEqual(list(Project.objects.filter(name="B-only")), [])

    def test_tenant_a_cannot_get_tenant_b_by_pk(self):
        b_project = Project.all_objects.get(name="B-only")
        with tenant_context(self.tenant_a):
            with self.assertRaises(Project.DoesNotExist):
                Project.objects.get(pk=b_project.pk)

    def test_no_tenant_bound_fails_closed(self):
        clear_current_tenant()
        self.assertEqual(list(Project.objects.all()), [])

    def test_unscoped_manager_sees_everything(self):
        self.assertEqual(Project.all_objects.count(), 2)


class TenantMiddlewareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Acme", slug="acme")
        Project.all_objects.create(tenant=cls.tenant_a, name="A-only")

    def tearDown(self):
        clear_current_tenant()

    def _run(self, **meta):
        captured = {}

        def view(request):
            captured["tenant"] = request.tenant
            captured["visible"] = list(
                Project.objects.values_list("name", flat=True)
            )
            return "ok"

        request = type("Req", (), {})()
        request.META = meta
        request.get_host = lambda: meta.get("HTTP_HOST", "testserver")
        TenantMiddleware(view)(request)
        return captured

    def test_binds_and_clears_from_jwt(self):
        result = self._run(HTTP_AUTHORIZATION=f"Bearer {make_tenant_token('acme')}")
        self.assertEqual(result["tenant"], self.tenant_a)
        self.assertEqual(result["visible"], ["A-only"])
        self.assertIsNone(get_current_tenant())  # cleared after the request

    def test_binds_from_subdomain(self):
        result = self._run(HTTP_HOST="acme.artikate.test")
        self.assertEqual(result["tenant"], self.tenant_a)

    def test_unknown_tenant_sees_nothing(self):
        result = self._run(HTTP_HOST="unknown.artikate.test")
        self.assertIsNone(result["tenant"])
        self.assertEqual(result["visible"], [])
