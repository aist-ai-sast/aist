/**
 * Tag naming rules, mirrored from the server so the user sees a rejection
 * while typing instead of after a failed request.
 *
 * The authority is `aist/api/tags.py`: the model stores tags through
 * `TagField(force_lowercase=True)` and a comma splits one tag into two, so the
 * same normalisation runs there regardless of what the client sends.
 */

export const SYSTEM_TAG_PREFIX = "aist:";
export const MAX_TAG_LENGTH = 64;
export const MAX_TAGS_PER_OBJECT = 50;

export function isSystemTag(tag: string): boolean {
  return tag.startsWith(SYSTEM_TAG_PREFIX);
}

export function normalizeTag(raw: string): string {
  return raw.trim().toLowerCase().split(/\s+/).filter(Boolean).join("-");
}

export type TagValidation = { tag: string; error?: undefined } | { tag?: undefined; error: string };

export function validateTag(raw: string, existing: string[] = []): TagValidation {
  if (raw.includes(",")) {
    return { error: "A comma separates tags — remove it." };
  }
  const tag = normalizeTag(raw);
  if (!tag) {
    return { error: "Enter a tag name." };
  }
  if (isSystemTag(tag)) {
    return { error: `The ${SYSTEM_TAG_PREFIX} prefix is reserved for the platform.` };
  }
  if (tag.length > MAX_TAG_LENGTH) {
    return { error: `Use at most ${MAX_TAG_LENGTH} characters.` };
  }
  if (!existing.includes(tag) && existing.length >= MAX_TAGS_PER_OBJECT) {
    return { error: `At most ${MAX_TAGS_PER_OBJECT} tags per object.` };
  }
  return { tag };
}
