from django.core.management.base import BaseCommand
from django.db import transaction

from tenants.models import Project, Tenant

DATA = {
    "acme": ("Acme", ["Acme Website Redesign", "Acme Mobile App"]),
    "globex": ("Globex", ["Globex Data Pipeline"]),
}


class Command(BaseCommand):
    help = "Create two demo tenants with projects to show tenant isolation."

    @transaction.atomic
    def handle(self, *args, **options):
        for slug, (name, projects) in DATA.items():
            tenant, _ = Tenant.objects.get_or_create(slug=slug, defaults={"name": name})
            for project_name in projects:
                # all_objects because objects is tenant-scoped and no tenant is bound here
                Project.all_objects.get_or_create(tenant=tenant, name=project_name)

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded tenants: acme (2 projects), globex (1 project).\n"
                "Try http://acme.artikate.test:8000/api/tenants/projects/ and "
                "http://globex.artikate.test:8000/api/tenants/projects/"
            )
        )
