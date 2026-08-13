import type { DastProjectBinding, DastTarget } from "./queries";

const REPOSITORY_TRIGGER = "repository-trigger";

/**
 * Whether a target's scenario declares a source repository, mirroring the backend's single
 * accessor (`DastProjectBinding.requires_source_repository`). Every screen asks this function
 * instead of testing `repository_keys` or `source_repo_key` for emptiness — the same rule the
 * backend enforces with a lint gate, for the same reason: one relaxed call site is how the
 * sourceless case silently drifts back out of support.
 */
export function targetRequiresSourceRepository(target: DastTarget): boolean {
  return target.launch_requirements.includes(REPOSITORY_TRIGGER);
}

/** Name one binding for an operator; a sourceless target has no repository to append. */
export function dastBindingLabel(binding: DastProjectBinding): string {
  const name = binding.target.display_name || binding.target.provider_id;
  if (!targetRequiresSourceRepository(binding.target)) return name;
  return `${name} · ${binding.source_repo_key}`;
}
