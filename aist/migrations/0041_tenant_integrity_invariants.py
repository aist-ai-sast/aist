from django.db import migrations


def audit_tenant_integrity(apps, schema_editor):
    AISTProject = apps.get_model("aist", "AISTProject")
    Organization = apps.get_model("aist", "Organization")
    OrgIntegration = apps.get_model("aist", "OrgIntegration")
    ProjectIntegrationOverride = apps.get_model("aist", "ProjectIntegrationOverride")
    WorkItemLink = apps.get_model("aist", "WorkItemLink")
    WorkItemProvider = apps.get_model("aist", "WorkItemProvider")

    errors = []
    project_organization_ids = {}
    for project in AISTProject.objects.select_related("product").iterator():
        canonical_id = (
            Organization.objects
            .filter(product_type_id=project.product.prod_type_id)
            .values_list("id", flat=True)
            .first()
        )
        project_organization_ids[project.pk] = canonical_id
        if project.organization_id is not None and project.organization_id != canonical_id:
            errors.append(
                f"AISTProject {project.pk}: organization={project.organization_id}, "
                f"product-type organization={canonical_id}",
            )

    for integration in OrgIntegration.objects.select_related("vpn_integration").iterator():
        vpn = integration.vpn_integration
        if vpn and (vpn.organization_id != integration.organization_id or vpn.integration_type != "VPN"):
            errors.append(f"OrgIntegration {integration.pk}: invalid VPN integration {vpn.pk}")

    for override in ProjectIntegrationOverride.objects.select_related("org_integration").iterator():
        integration = override.org_integration
        if integration and (
            integration.organization_id != project_organization_ids.get(override.project_id)
            or integration.integration_type != override.integration_type
        ):
            errors.append(f"ProjectIntegrationOverride {override.pk}: invalid integration {integration.pk}")

    for provider in WorkItemProvider.objects.select_related("vpn_integration").iterator():
        vpn = provider.vpn_integration
        if vpn and (vpn.organization_id != provider.organization_id or vpn.integration_type != "VPN"):
            errors.append(f"WorkItemProvider {provider.pk}: invalid VPN integration {vpn.pk}")

    for link in WorkItemLink.objects.filter(provider__isnull=False).select_related(
        "provider", "finding__test__engagement__product",
    ).iterator():
        product_type_id = link.finding.test.engagement.product.prod_type_id
        finding_organization_id = (
            Organization.objects
            .filter(product_type_id=product_type_id)
            .values_list("id", flat=True)
            .first()
        )
        if finding_organization_id != link.provider.organization_id:
            errors.append(
                f"WorkItemLink {link.pk}: provider organization={link.provider.organization_id}, "
                f"finding organization={finding_organization_id}",
            )

    if errors:
        preview = "; ".join(errors[:20])
        suffix = f"; and {len(errors) - 20} more" if len(errors) > 20 else ""
        msg = (
            "Tenant-integrity migration stopped because existing rows cross an "
            f"organization boundary: {preview}{suffix}"
        )
        raise RuntimeError(msg)


def restore_project_organization_mirror(apps, schema_editor):
    AISTProject = apps.get_model("aist", "AISTProject")
    Organization = apps.get_model("aist", "Organization")
    for organization in Organization.objects.exclude(product_type_id=None).iterator():
        AISTProject.objects.filter(
            product__prod_type_id=organization.product_type_id,
        ).update(organization_id=organization.pk)


CREATE_SQL = r"""
CREATE OR REPLACE FUNCTION aist_protect_organization_product_type()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.product_type_id IS NOT NULL THEN
            PERFORM pg_advisory_xact_lock(41917, NEW.product_type_id);
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.product_type_id IS NOT DISTINCT FROM NEW.product_type_id THEN
        RETURN NEW;
    END IF;

    IF OLD.product_type_id IS NOT NULL AND NEW.product_type_id IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(41917, LEAST(OLD.product_type_id, NEW.product_type_id));
        PERFORM pg_advisory_xact_lock(41917, GREATEST(OLD.product_type_id, NEW.product_type_id));
    ELSE
        PERFORM pg_advisory_xact_lock(41917, COALESCE(OLD.product_type_id, NEW.product_type_id));
    END IF;

    -- Linking a previously-unowned Product_Type is an explicit ownership
    -- assignment and is allowed. Once an owned tenant path has projects,
    -- however, moving either endpoint would silently re-home those projects.
    IF OLD.product_type_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM aist_aistproject AS project
          JOIN dojo_product AS product ON product.id = project.product_id
         WHERE product.prod_type_id = OLD.product_type_id
            OR product.prod_type_id = NEW.product_type_id
    ) THEN
        RAISE EXCEPTION 'Organization Product_Type cannot change while AIST projects depend on it'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_organization_product_type_immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_organization_product_type_immutable
BEFORE INSERT OR UPDATE OF product_type_id ON aist_organization
FOR EACH ROW EXECUTE FUNCTION aist_protect_organization_product_type();

CREATE OR REPLACE FUNCTION aist_protect_organization_delete()
RETURNS trigger AS $$
BEGIN
    IF OLD.product_type_id IS NOT NULL AND EXISTS (
        SELECT 1
          FROM aist_aistproject AS project
          JOIN dojo_product AS product ON product.id = project.product_id
         WHERE product.prod_type_id = OLD.product_type_id
    ) THEN
        RAISE EXCEPTION 'Organization cannot be deleted while AIST projects depend on its Product_Type'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_organization_tenant_delete_protected';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_organization_tenant_delete_protected
BEFORE DELETE ON aist_organization
FOR EACH ROW EXECUTE FUNCTION aist_protect_organization_delete();

CREATE OR REPLACE FUNCTION aist_protect_project_product()
RETURNS trigger AS $$
BEGIN
    IF OLD.product_id IS DISTINCT FROM NEW.product_id THEN
        RAISE EXCEPTION 'AISTProject Product is immutable because it defines the tenant path'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_project_product_immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_project_product_immutable
BEFORE UPDATE OF product_id ON aist_aistproject
FOR EACH ROW EXECUTE FUNCTION aist_protect_project_product();

CREATE OR REPLACE FUNCTION aist_protect_product_tenant()
RETURNS trigger AS $$
BEGIN
    IF OLD.prod_type_id IS NOT DISTINCT FROM NEW.prod_type_id THEN
        RETURN NEW;
    END IF;

    PERFORM pg_advisory_xact_lock(41917, LEAST(OLD.prod_type_id, NEW.prod_type_id));
    PERFORM pg_advisory_xact_lock(41917, GREATEST(OLD.prod_type_id, NEW.prod_type_id));

    IF EXISTS (SELECT 1 FROM aist_aistproject WHERE product_id = OLD.id)
       OR EXISTS (
           SELECT 1
             FROM aist_workitemlink AS link
             JOIN dojo_finding AS finding ON finding.id = link.finding_id
             JOIN dojo_test AS test ON test.id = finding.test_id
             JOIN dojo_engagement AS engagement ON engagement.id = test.engagement_id
            WHERE engagement.product_id = OLD.id AND link.provider_id IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'Product tenant cannot change while tenant-owned AIST data depends on it'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_product_tenant_immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_product_tenant_immutable
BEFORE UPDATE OF prod_type_id ON dojo_product
FOR EACH ROW EXECUTE FUNCTION aist_protect_product_tenant();

CREATE OR REPLACE FUNCTION aist_enforce_project_integration_tenant()
RETURNS trigger AS $$
DECLARE
    project_organization_id bigint;
    integration_organization_id bigint;
    selected_integration_type varchar(32);
BEGIN
    IF NEW.org_integration_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT organization.id INTO project_organization_id
      FROM aist_aistproject AS project
      JOIN dojo_product AS product ON product.id = project.product_id
      LEFT JOIN aist_organization AS organization ON organization.product_type_id = product.prod_type_id
     WHERE project.id = NEW.project_id
     FOR SHARE OF project, product;
    SELECT organization_id, integration_type
      INTO integration_organization_id, selected_integration_type
      FROM aist_orgintegration WHERE id = NEW.org_integration_id FOR SHARE;
    IF project_organization_id IS NULL
       OR project_organization_id IS DISTINCT FROM integration_organization_id
       OR NEW.integration_type IS DISTINCT FROM selected_integration_type THEN
        RAISE EXCEPTION 'Project integration override crosses its tenant or integration type boundary'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_project_integration_tenant_match';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_project_integration_tenant_match
BEFORE INSERT OR UPDATE OF project_id, integration_type, org_integration_id
ON aist_projectintegrationoverride
FOR EACH ROW EXECUTE FUNCTION aist_enforce_project_integration_tenant();

CREATE OR REPLACE FUNCTION aist_enforce_org_integration_tenant()
RETURNS trigger AS $$
DECLARE
    vpn_organization_id bigint;
    vpn_type varchar(32);
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.organization_id IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION 'OrgIntegration organization is immutable'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_org_integration_organization_immutable';
    END IF;

    IF NEW.vpn_integration_id IS NOT NULL THEN
        SELECT organization_id, integration_type
          INTO vpn_organization_id, vpn_type
          FROM aist_orgintegration WHERE id = NEW.vpn_integration_id FOR SHARE;
        IF vpn_organization_id IS DISTINCT FROM NEW.organization_id OR vpn_type IS DISTINCT FROM 'VPN' THEN
            RAISE EXCEPTION 'OrgIntegration VPN must be a VPN in the same tenant'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_org_integration_vpn_tenant_match';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1 FROM aist_projectintegrationoverride AS override
         JOIN aist_aistproject AS project ON project.id = override.project_id
         JOIN dojo_product AS product ON product.id = project.product_id
         LEFT JOIN aist_organization AS organization ON organization.product_type_id = product.prod_type_id
        WHERE override.org_integration_id = NEW.id
          AND (organization.id IS DISTINCT FROM NEW.organization_id
               OR override.integration_type IS DISTINCT FROM NEW.integration_type)
    ) OR EXISTS (
        SELECT 1 FROM aist_orgintegration AS dependent
        WHERE dependent.vpn_integration_id = NEW.id
          AND (dependent.organization_id IS DISTINCT FROM NEW.organization_id
               OR NEW.integration_type IS DISTINCT FROM 'VPN')
    ) OR EXISTS (
        SELECT 1 FROM aist_workitemprovider AS provider
        WHERE provider.vpn_integration_id = NEW.id
          AND (provider.organization_id IS DISTINCT FROM NEW.organization_id
               OR NEW.integration_type IS DISTINCT FROM 'VPN')
    ) THEN
        RAISE EXCEPTION 'OrgIntegration change would invalidate a tenant-owned reference'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_org_integration_dependents_valid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_org_integration_tenant_match
BEFORE INSERT OR UPDATE OF organization_id, integration_type, vpn_integration_id
ON aist_orgintegration
FOR EACH ROW EXECUTE FUNCTION aist_enforce_org_integration_tenant();

CREATE OR REPLACE FUNCTION aist_enforce_work_item_provider_tenant()
RETURNS trigger AS $$
DECLARE
    vpn_organization_id bigint;
    vpn_type varchar(32);
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.organization_id IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION 'WorkItemProvider organization is immutable'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_work_item_provider_organization_immutable';
    END IF;
    IF NEW.vpn_integration_id IS NOT NULL THEN
        SELECT organization_id, integration_type
          INTO vpn_organization_id, vpn_type
          FROM aist_orgintegration WHERE id = NEW.vpn_integration_id FOR SHARE;
        IF vpn_organization_id IS DISTINCT FROM NEW.organization_id OR vpn_type IS DISTINCT FROM 'VPN' THEN
            RAISE EXCEPTION 'WorkItemProvider VPN must be a VPN in the same tenant'
                USING ERRCODE = '23514', CONSTRAINT = 'aist_work_item_provider_vpn_tenant_match';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_work_item_provider_tenant_match
BEFORE INSERT OR UPDATE OF organization_id, vpn_integration_id ON aist_workitemprovider
FOR EACH ROW EXECUTE FUNCTION aist_enforce_work_item_provider_tenant();

CREATE OR REPLACE FUNCTION aist_enforce_work_item_link_tenant()
RETURNS trigger AS $$
DECLARE
    finding_organization_id bigint;
    provider_organization_id bigint;
BEGIN
    IF NEW.provider_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT organization.id
      INTO finding_organization_id
      FROM dojo_finding AS finding
      JOIN dojo_test AS test ON test.id = finding.test_id
      JOIN dojo_engagement AS engagement ON engagement.id = test.engagement_id
      JOIN dojo_product AS product ON product.id = engagement.product_id
      LEFT JOIN aist_organization AS organization ON organization.product_type_id = product.prod_type_id
     WHERE finding.id = NEW.finding_id
     FOR SHARE OF finding, test, engagement, product;

    SELECT organization_id INTO provider_organization_id
      FROM aist_workitemprovider WHERE id = NEW.provider_id FOR SHARE;

    IF finding_organization_id IS NULL
       OR finding_organization_id IS DISTINCT FROM provider_organization_id THEN
        RAISE EXCEPTION 'WorkItemLink provider must belong to the finding tenant'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_work_item_link_tenant_match';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_work_item_link_tenant_match
BEFORE INSERT OR UPDATE OF finding_id, provider_id ON aist_workitemlink
FOR EACH ROW EXECUTE FUNCTION aist_enforce_work_item_link_tenant();

CREATE OR REPLACE FUNCTION aist_protect_finding_tenant_path()
RETURNS trigger AS $$
BEGIN
    IF OLD.test_id IS DISTINCT FROM NEW.test_id
       AND EXISTS (
           SELECT 1 FROM aist_workitemlink
            WHERE finding_id = OLD.id AND provider_id IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'Finding tenant path cannot change while provider-backed links exist'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_finding_tenant_path_immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_finding_tenant_path_immutable
BEFORE UPDATE OF test_id ON dojo_finding
FOR EACH ROW EXECUTE FUNCTION aist_protect_finding_tenant_path();

CREATE OR REPLACE FUNCTION aist_protect_test_tenant_path()
RETURNS trigger AS $$
BEGIN
    IF OLD.engagement_id IS DISTINCT FROM NEW.engagement_id
       AND EXISTS (
           SELECT 1
             FROM aist_workitemlink AS link
             JOIN dojo_finding AS finding ON finding.id = link.finding_id
            WHERE finding.test_id = OLD.id AND link.provider_id IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'Test tenant path cannot change while provider-backed links exist'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_test_tenant_path_immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_test_tenant_path_immutable
BEFORE UPDATE OF engagement_id ON dojo_test
FOR EACH ROW EXECUTE FUNCTION aist_protect_test_tenant_path();

CREATE OR REPLACE FUNCTION aist_protect_engagement_tenant_path()
RETURNS trigger AS $$
BEGIN
    IF OLD.product_id IS DISTINCT FROM NEW.product_id
       AND EXISTS (
           SELECT 1
             FROM aist_workitemlink AS link
             JOIN dojo_finding AS finding ON finding.id = link.finding_id
             JOIN dojo_test AS test ON test.id = finding.test_id
            WHERE test.engagement_id = OLD.id AND link.provider_id IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'Engagement tenant path cannot change while provider-backed links exist'
            USING ERRCODE = '23514', CONSTRAINT = 'aist_engagement_tenant_path_immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER aist_engagement_tenant_path_immutable
BEFORE UPDATE OF product_id ON dojo_engagement
FOR EACH ROW EXECUTE FUNCTION aist_protect_engagement_tenant_path();
"""


DROP_SQL = r"""
DROP TRIGGER IF EXISTS aist_engagement_tenant_path_immutable ON dojo_engagement;
DROP TRIGGER IF EXISTS aist_test_tenant_path_immutable ON dojo_test;
DROP TRIGGER IF EXISTS aist_finding_tenant_path_immutable ON dojo_finding;
DROP TRIGGER IF EXISTS aist_work_item_link_tenant_match ON aist_workitemlink;
DROP TRIGGER IF EXISTS aist_work_item_provider_tenant_match ON aist_workitemprovider;
DROP TRIGGER IF EXISTS aist_org_integration_tenant_match ON aist_orgintegration;
DROP TRIGGER IF EXISTS aist_project_integration_tenant_match ON aist_projectintegrationoverride;
DROP TRIGGER IF EXISTS aist_product_tenant_immutable ON dojo_product;
DROP TRIGGER IF EXISTS aist_project_product_immutable ON aist_aistproject;
DROP TRIGGER IF EXISTS aist_organization_tenant_delete_protected ON aist_organization;
DROP TRIGGER IF EXISTS aist_organization_product_type_immutable ON aist_organization;
DROP FUNCTION IF EXISTS aist_protect_engagement_tenant_path();
DROP FUNCTION IF EXISTS aist_protect_test_tenant_path();
DROP FUNCTION IF EXISTS aist_protect_finding_tenant_path();
DROP FUNCTION IF EXISTS aist_enforce_work_item_link_tenant();
DROP FUNCTION IF EXISTS aist_enforce_work_item_provider_tenant();
DROP FUNCTION IF EXISTS aist_enforce_org_integration_tenant();
DROP FUNCTION IF EXISTS aist_enforce_project_integration_tenant();
DROP FUNCTION IF EXISTS aist_protect_product_tenant();
DROP FUNCTION IF EXISTS aist_protect_project_product();
DROP FUNCTION IF EXISTS aist_protect_organization_delete();
DROP FUNCTION IF EXISTS aist_protect_organization_product_type();
"""


class Migration(migrations.Migration):
    dependencies = [("aist", "0040_aist_api_token_organization")]

    operations = [
        migrations.RunPython(audit_tenant_integrity, restore_project_organization_mirror),
        migrations.RemoveField(model_name="aistproject", name="organization"),
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
