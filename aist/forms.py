from __future__ import annotations

import json

from django import forms

from aist.ai_filter import validate_and_normalize_filter
from aist.integrations.dast_readiness import check_dast_launch_readiness
from aist.models import AISTProject, AISTProjectVersion, DastProjectBinding, VersionType
from aist.pipeline_args import PipelineArguments
from aist.utils.pipeline import has_unfinished_pipeline
from aist.utils.pipeline_imports import _load_analyzers_config


class AISTProjectVersionForm(forms.ModelForm):
    class Meta:
        model = AISTProjectVersion
        fields = ["project", "version_type", "version", "description", "source_archive", "metadata"]
        widgets = {
            "project": forms.HiddenInput,
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        cleaned = super().clean()
        version_type = cleaned.get("version_type")
        src = cleaned.get("source_archive")
        version = (cleaned.get("version") or "").strip()
        cleaned["version"] = version

        if version_type == VersionType.FILE_HASH:
            if not src:
                self.add_error("source_archive", "Archive is required for FILE_HASH.")
        elif not version:
            self.add_error("version", "Git ref is required for GIT_BRANCH/GIT_HASH.")

        proj = cleaned.get("project")
        if proj and version:
            if AISTProjectVersion.objects.filter(
                project=proj,
                version=version,
                version_type=version_type,
            ).exists():
                self.add_error("version", "This version already exists for the selected project.")

        return cleaned


def _signature(project_id: str | None, langs: list[str], time_class: str | None) -> str:
    return f"{project_id or ''}::{time_class or 'slow'}::{','.join(sorted(set(langs or [])))}"


class _AISTPipelineArgsBaseForm(forms.Form):

    """
    Shared pipeline-args form. This is the ONLY place where:
    - analyzers/languages/time-class fields are defined
    - dynamic defaults are calculated (signature -> defaults)
    - bootstrap classes are applied

    Consumers:
    - AISTPipelineRunForm (adds project + run-specific validation)
    - AISTLaunchConfigForm (adds name/description/is_default, no run-specific validation)
    """

    project_version = forms.ModelChoiceField(
        queryset=AISTProjectVersion.objects.none(),
        label="Project version",
        required=False,
        help_text="By default will be used latest commit on master branch",
    )
    rebuild_images = forms.BooleanField(required=False, initial=False, label="Rebuild images")
    log_level = forms.ChoiceField(
        choices=[("INFO", "INFO"), ("DEBUG", "DEBUG"), ("WARNING", "WARNING"), ("ERROR", "ERROR")],
        initial="INFO",
        label="Log level",
    )
    languages = forms.MultipleChoiceField(choices=[], required=False, label="Languages", widget=forms.CheckboxSelectMultiple)
    analyzers = forms.MultipleChoiceField(choices=[], required=False, label="Analyzers to launch", widget=forms.CheckboxSelectMultiple)
    time_class_level = forms.ChoiceField(choices=[], required=False, label="Maximum time class", initial="slow")
    selection_signature = forms.CharField(required=False, widget=forms.HiddenInput)

    AI_MODE_CHOICES = (
        ("MANUAL", "Manual selection of findings for AI"),
        ("AUTO_DEFAULT", "Send findings to AI automatically with configured filter"),
    )
    ai_mode = forms.ChoiceField(
        label="AI triage",
        choices=AI_MODE_CHOICES,
        widget=forms.RadioSelect,
        initial="MANUAL",
        required=True,
    )
    ai_filter_json = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "style": "font-family: Menlo, Monaco, Consolas, 'Courier New', monospace; font-size: 12px;",
            },
        ),
    )

    def clean(self):
        cleaned = super().clean()
        ai_mode = cleaned.get("ai_mode") or "MANUAL"
        raw_json = (cleaned.get("ai_filter_json") or "").strip()

        cleaned["ai_filter_snapshot"] = None
        if ai_mode != "AUTO_DEFAULT":
            return cleaned

        if not raw_json:
            self.add_error("ai_filter_json", "AI filter is required for AUTO_DEFAULT.")
            return cleaned

        try:
            parsed = json.loads(raw_json)
        except Exception as exc:
            self.add_error("ai_filter_json", f"AI filter JSON is invalid: {exc}")
            return cleaned

        try:
            cleaned["ai_filter_snapshot"] = validate_and_normalize_filter(parsed)
        except Exception as exc:
            self.add_error("ai_filter_json", f"AI filter is invalid: {exc}")

        return cleaned

    def __init__(self, *args, **kwargs):
        # Optional: project passed explicitly (for UI where project is fixed and not a form field)
        self._fixed_project: AISTProject | None = kwargs.pop("project", None)
        super().__init__(*args, **kwargs)

        # Bootstrap classes (exactly once for all consumers)
        if "project" in self.fields:
            self.fields["project"].widget.attrs.update({"class": "form-select"})
        self.fields["log_level"].widget.attrs.update({"class": "form-select"})
        self.fields["time_class_level"].widget.attrs.update({"class": "form-select"})
        self.fields["rebuild_images"].widget.attrs.update({"class": "form-check-input"})
        self.fields["languages"].widget.attrs.update({"class": "form-check-input"})
        self.fields["analyzers"].widget.attrs.update({"class": "form-check-input"})
        self.fields["project_version"].widget.attrs.update({"class": "form-select"})
        self.fields["project_version"].empty_label = "Use default (latest on default branch)"
        self.fields["project_version"].queryset = AISTProjectVersion.objects.none()
        self.fields["ai_mode"].widget.attrs.update({"class": "form-check-input"})

        cfg = _load_analyzers_config()
        if cfg:
            self.fields["languages"].choices = [(x, x) for x in cfg.get_supported_languages()]
            self.fields["analyzers"].choices = [(x, x) for x in cfg.get_supported_analyzers()]
            self.fields["time_class_level"].choices = [(x, x) for x in cfg.get_analyzers_time_class()]

        # If not bound - nothing to compute (keeps existing behavior) :contentReference[oaicite:1]{index=1}
        if not self.is_bound:
            return

        proj = self._resolve_project_for_dynamic_defaults()
        if proj:
            self.fields["project_version"].queryset = proj.versions.all()

        posted_langs = self.data.getlist(self.add_prefix("languages"))
        project_supported_languages = (proj.supported_languages if proj else []) or []
        langs_union = list(set((posted_langs or []) + project_supported_languages))

        time_class = self.data.get(self.add_prefix("time_class_level")) or "slow"

        # Signature-driven defaults (same logic as before) :contentReference[oaicite:2]{index=2}
        project_id = str(proj.id) if proj else None
        posted_sig = self.data.get(self.add_prefix("selection_signature")) or ""
        new_sig = _signature(project_id, langs_union, time_class)
        self.initial["selection_signature"] = new_sig
        posted_analyzers = self.data.getlist(self.add_prefix("analyzers"))

        defaults = []
        if cfg and proj:
            non_compile_project = not proj.compilable
            filtered = cfg.get_filtered_analyzers(
                analyzers_to_run=None,
                max_time_class=time_class,
                non_compile_project=non_compile_project,
                target_languages=langs_union,
                show_only_parent=True,
            )
            defaults = cfg.get_names(filtered)

        # Keep explicit user selection on submit; only inject defaults when analyzers are absent.
        if posted_sig != new_sig and not posted_analyzers:
            qd = self.data.copy()
            qd.setlist(self.add_prefix("analyzers"), defaults)
            self.data = qd
            self.initial["analyzers"] = defaults
        else:
            self.initial["analyzers"] = posted_analyzers or defaults

    def _resolve_project_for_dynamic_defaults(self) -> AISTProject | None:
        """
        Used only for UI conveniences (versions queryset + default analyzers).
        - In run-form project comes from bound form field
        - In launch-config form project is passed via __init__(project=...)
        """
        if self._fixed_project:
            return self._fixed_project

        if "project" not in self.fields:
            return None

        project_id = self.data.get(self.add_prefix("project")) or None
        if not project_id:
            return None
        try:
            return AISTProject.objects.get(id=project_id)
        except AISTProject.DoesNotExist:
            return None

    def get_params_payload(self, *, project: AISTProject) -> dict:
        """
        Common payload collector for BOTH modes.
        SSOT validation/defaulting is PipelineArguments.normalize_params (same as API). :contentReference[oaicite:3]{index=3}
        """
        pv: AISTProjectVersion | None = self.cleaned_data.get("project_version")
        raw = {
            "rebuild_images": self.cleaned_data.get("rebuild_images") or False,
            "log_level": self.cleaned_data.get("log_level") or "INFO",
            "selected_languages": self.cleaned_data.get("languages") or [],
            "analyzers": self.cleaned_data.get("analyzers") or [],
            # Keep existing UI behavior: time_class_level ignored when analyzers explicitly selected :contentReference[oaicite:4]{index=4}
            "time_class_level": None,
            "ai_mode": self.cleaned_data.get("ai_mode") or "MANUAL",
            # ai_filter_snapshot is parsed/validated in clean() for AUTO_DEFAULT
            "ai_filter_snapshot": self.cleaned_data.get("ai_filter_snapshot"),
            "project_version": (pv.as_dict() if pv else None),
        }
        return PipelineArguments.normalize_params(project=project, raw_params=raw)


class AISTPipelineRunForm(_AISTPipelineArgsBaseForm):
    project = forms.ModelChoiceField(
        queryset=AISTProject.objects.all(),
        label="Project",
        help_text="Choose a pre-configured SAST project",
        required=True,
    )

    def clean(self):
        cleaned = super().clean()

        project: AISTProject | None = cleaned.get("project")
        if not project:
            return cleaned

        project_version: AISTProjectVersion | None = cleaned.get("project_version")
        if not project_version:
            project_version = (
                project.versions
                .filter(version_type=VersionType.GIT_BRANCH)
                .order_by("-updated", "-created")
                .first()
            )
            if project_version is None:
                project_version = project.versions.order_by("-updated", "-created").first()
            cleaned["project_version"] = project_version

        if project_version and has_unfinished_pipeline(project_version):
            self.add_error(
                "project_version",
                "There is already a running pipeline for this project version.",
            )
            return cleaned

        return cleaned

    def get_params(self) -> dict:
        proj: AISTProject = self.cleaned_data["project"]
        # Use shared collector; this keeps PipelineArguments.normalize_params as SSOT
        return self.get_params_payload(project=proj)


class ProjectScopedSelect(forms.Select):

    """
    Select widget that stamps data-project (and optional data-meta JSON) attributes onto
    each <option>, so client-side JS can filter options to the chosen project and render a
    per-option hint without a second round trip to the server.
    """

    def __init__(self, *args, option_project_ids=None, option_meta=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._option_project_ids = option_project_ids or {}
        self._option_meta = option_meta or {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        key = str(value)
        project_id = self._option_project_ids.get(key)
        if project_id is not None:
            option["attrs"]["data-project"] = str(project_id)
        meta = self._option_meta.get(key)
        if meta is not None:
            option["attrs"]["data-meta"] = json.dumps(meta)
        return option


class DastPipelineRunForm(forms.Form):

    """Validate an ephemeral DAST launch without creating a saved preset."""

    project = forms.ModelChoiceField(
        queryset=AISTProject.objects.none(),
        label="Project",
        required=True,
    )
    dast_binding = forms.ModelChoiceField(
        queryset=DastProjectBinding.objects.none(),
        label="DAST target binding",
        required=True,
        help_text="Only bindings for the selected project are usable; pick a project first.",
    )
    trigger_project_version = forms.ModelChoiceField(
        queryset=AISTProjectVersion.objects.none(),
        label="Git source version",
        required=True,
        help_text="Only Git versions for the selected project are usable; pick a project first.",
    )
    parameters = forms.JSONField(
        label="Target parameters",
        required=False,
        initial=dict,
        widget=forms.Textarea(attrs={"rows": 10, "class": "form-control font-monospace"}),
        help_text="JSON object validated against the selected target's current parameter schema.",
    )

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        projects = project_queryset if project_queryset is not None else AISTProject.objects.none()
        self.fields["project"].queryset = projects
        self.fields["project"].widget.attrs.update({"class": "form-select"})
        project_ids = projects.values_list("pk", flat=True)

        bindings_qs = (
            DastProjectBinding.objects
            .filter(project_id__in=project_ids, enabled=True)
            .select_related("project__product", "target")
            .order_by("project__product__name", "target__display_name")
        )
        bindings = list(bindings_qs)
        self.fields["dast_binding"].label_from_instance = lambda b: (
            f"{b.project.product.name} · {b.target.display_name or b.target.provider_id} — {b.source_repo_key}"
        )
        # The widget must be swapped in before `.queryset` is assigned: the queryset
        # property setter is what pushes rendered choices onto `field.widget`, and it
        # only reaches whichever widget instance is current at that moment.
        self.fields["dast_binding"].widget = ProjectScopedSelect(
            attrs={"class": "form-select"},
            option_project_ids={str(b.pk): b.project_id for b in bindings},
            option_meta={
                str(b.pk): {
                    "schema": b.target.parameter_schema or {},
                    "defaults": b.target.provider_defaults or {},
                }
                for b in bindings
            },
        )
        self.fields["dast_binding"].queryset = bindings_qs

        versions_qs = (
            AISTProjectVersion.objects
            .filter(
                project_id__in=project_ids,
                version_type__in=[VersionType.GIT_BRANCH, VersionType.GIT_HASH],
            )
            .select_related("project__product")
            .order_by("project__product__name", "-updated")
        )
        versions = list(versions_qs)
        self.fields["trigger_project_version"].label_from_instance = lambda v: (
            f"{v.project.product.name} · {v.version} ({v.get_version_type_display()})"
        )
        self.fields["trigger_project_version"].widget = ProjectScopedSelect(
            attrs={"class": "form-select"},
            option_project_ids={str(v.pk): v.project_id for v in versions},
        )
        self.fields["trigger_project_version"].queryset = versions_qs
        self.arguments: PipelineArguments | None = None

    def clean(self):
        cleaned = super().clean()
        project = cleaned.get("project")
        binding = cleaned.get("dast_binding")
        trigger = cleaned.get("trigger_project_version")
        if not project or not binding or not trigger:
            return cleaned
        if binding.project_id != project.pk:
            self.add_error("dast_binding", "The DAST binding must belong to the selected project.")
            return cleaned
        if trigger.project_id != project.pk:
            self.add_error("trigger_project_version", "The Git source version must belong to the selected project.")
            return cleaned
        try:
            arguments = PipelineArguments.for_dast(
                project=project,
                binding=binding,
                trigger_project_version=trigger,
                raw_params=cleaned.get("parameters") or {},
            )
        except (TypeError, ValueError) as exc:
            self.add_error("parameters", str(exc))
            return cleaned
        readiness = check_dast_launch_readiness(arguments)
        if not readiness.ready:
            detail = "; ".join(issue.detail for issue in readiness.issues)
            self.add_error(None, detail or "The selected DAST binding is not ready.")
            return cleaned
        self.arguments = arguments
        return cleaned

    def get_arguments(self) -> PipelineArguments:
        if self.arguments is None:
            msg = "DAST form must be valid before reading launch arguments."
            raise ValueError(msg)
        return self.arguments


class AISTLaunchConfigForm(_AISTPipelineArgsBaseForm):

    """
    Thin form for LaunchConfig UI: only adds metadata fields, reuses ALL pipeline args
    from _AISTPipelineArgsBaseForm (no duplication).
    """

    name = forms.CharField(label="Name", max_length=128, required=True)
    description = forms.CharField(label="Description", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    is_default = forms.BooleanField(label="Make default", required=False, initial=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs.update({"class": "form-control"})
        self.fields["description"].widget.attrs.update({"class": "form-control"})
        self.fields["is_default"].widget.attrs.update({"class": "form-check-input"})
        # launch-config creation must NOT block on unfinished pipelines (that is run-only rule)

    def to_api_create_payload(self, *, project: AISTProject) -> dict:
        params = self.get_params_payload(project=project)
        return {
            "name": self.cleaned_data["name"],
            "description": self.cleaned_data.get("description") or "",
            "is_default": bool(self.cleaned_data.get("is_default") or False),
            "params": params,
        }
