/**
 * Shared helpers for building a tool/skill item's editable form values.
 *
 * Used by both the Setup Wizard's tool step (`StepToolConfig.vue`) and the
 * Settings "Tools & Skills" page (`AgentsToolsSettings.vue`) so the two don't
 * carry byte-identical copies of this logic.
 */
import type { ProfileValue } from '../stores/settings';
import type { JsonSchema } from '../services/agentApi';

/** Default empty value for a form field of the given JSON-schema type. */
export function getDefaultForType(type: string): ProfileValue {
  if (type === 'number') return 0;
  if (type === 'boolean') return false;
  return '';
}

/**
 * Seed variable values from a fields spec's defaults, then overlay any stored
 * (persisted) values so user overrides win.
 */
export function initVarValues(
  fields: Record<string, { default?: unknown }> | undefined,
  stored: Record<string, string> | undefined,
): Record<string, string> {
  const values: Record<string, string> = {};
  if (fields) {
    for (const [key, field] of Object.entries(fields)) {
      if (field.default != null) values[key] = String(field.default);
    }
  }
  if (stored) Object.assign(values, stored);
  return values;
}

/** Seed argument values from a JSON schema, overlaying any stored values. */
export function initArgValues(
  schema: JsonSchema | null,
  storedValues?: Record<string, unknown> | null,
): Record<string, ProfileValue> {
  if (!schema || !schema.properties) return {};
  const stored = storedValues || {};
  const values: Record<string, ProfileValue> = {};
  for (const [propName, propDef] of Object.entries(schema.properties)) {
    values[propName] = (stored[propName] as ProfileValue) ?? getDefaultForType(propDef.type);
  }
  return values;
}
