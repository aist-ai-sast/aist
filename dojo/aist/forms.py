from __future__ import annotations

from django import forms

from dojo.aist.ai_filter import get_required_ai_filter_for_start
from dojo.aist.models import AISTProject, AISTProjectVersion, VersionType
from dojo.aist.pipeline_args import PipelineArguments
from dojo.aist.utils.pipeline import has_unfinished_pipeline
from dojo.aist.utils.pipeline_imports import _load_analyzers_config


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
            self.add_error("version", "Git hash / ref is required for GIT_HASH.")

        proj = cleaned.get("project")
        if proj and version:
            if AISTProjectVersion.objects.filter(project=proj, version=version).exists():
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
        ("AUTO_DEFAULT", "Send findings to AI automatically with default filter"),
    )
    ai_mode = forms.ChoiceField(
        label="AI triage",
        choices=AI_MODE_CHOICES,
        widget=forms.RadioSelect,
        initial="MANUAL",
        required=True,
    )
    ai_filter_json = forms.CharField(required=False, widget=forms.HiddenInput)

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

        if posted_sig != new_sig:
            qd = self.data.copy()
            qd.setlist(self.add_prefix("analyzers"), defaults)
            self.data = qd
            self.initial["analyzers"] = defaults
        else:
            self.initial["analyzers"] = self.data.getlist(self.add_prefix("analyzers")) or defaults

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
            # run-form will set ai_filter_snapshot in clean(); launch-config keeps None and lets API SSOT decide
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

    # keep run-specific computed value
    ai_filter_snapshot = None  # injected into cleaned_data in clean()

    def clean(self):
        cleaned = super().clean()

        project: AISTProject | None = cleaned.get("project")
        if not project:
            return cleaned

        project_version: AISTProjectVersion | None = cleaned.get("project_version")
        if not project_version:
            project_version = project.versions.order_by("-created").first()
            cleaned["project_version"] = project_version

        if project_version and has_unfinished_pipeline(project_version):
            self.add_error(
                "project_version",
                "There is already a running pipeline for this project version.",
            )
            return cleaned

        ai_mode = cleaned.get("ai_mode") or "MANUAL"
        cleaned["ai_filter_snapshot"] = None

        if ai_mode == "AUTO_DEFAULT":
            try:
                _, default_filter = get_required_ai_filter_for_start(project=project, provided_filter=None)
            except Exception as e:
                self.add_error(None, f"AI filter is invalid: {e}")
                return cleaned

            cleaned["ai_filter_snapshot"] = default_filter

        return cleaned

    def get_params(self) -> dict:
        proj: AISTProject = self.cleaned_data["project"]
        # Use shared collector; this keeps PipelineArguments.normalize_params as SSOT
        return self.get_params_payload(project=proj)


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
