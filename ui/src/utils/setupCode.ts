// Setup codes: the QR / typed label of a Cremind Tag bridge or tag
// (the contract's docs/connect-setup.md §2.2, reference implementation
// app/tags/runtime/secure/codes.py — keep the two in step).
//
//   payload (15 B) = (0x20 | role) u8 ‖ short_id u32le ‖ setup_secret[10]
//   code           = Crockford base32 of the payload (24 symbols, big-endian
//                    bit order) ‖ one GF(32) check symbol
//   display        = XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
//   QR text        = "CTAG:" ‖ code
//
// The check symbol is c = Σ α^(i+1)·v_i over GF(32) (modulus x⁵+x²+1, α = 2)
// for the 24 data symbols v_i: every single mistyped symbol and every swap of
// two neighbouring symbols changes it, so a typo is caught here before
// anything is sent.
//
// The page only validates a code and shows its short id; the code text itself
// goes to the server, which does the pairing. A code is a pairing credential:
// never log it, and drop it once the dialog that asked for it closes.

export type SetupRole = 'bridge' | 'tag';

/** `node_roles` in the protocol spec. Gateways have no setup code. */
export const SETUP_ROLE_IDS: Readonly<Record<SetupRole, number>> = { bridge: 2, tag: 3 };

export const SETUP_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
export const SETUP_QR_PREFIX = 'CTAG:';
/** Symbols in a code: 24 data symbols and the check symbol. */
export const SETUP_CODE_LENGTH = 25;
export const SETUP_PAYLOAD_LENGTH = 15;
const DATA_SYMBOLS = 24;
const GF32_MODULUS = 0x25;
const VERSION_NIBBLE = 0x20;

const ALIASES: Readonly<Record<string, string>> = { O: '0', I: '1', L: '1' };
const SKIPPED = new Set([' ', '-', '\t', '\r', '\n', '_']);
const VALUE_OF: ReadonlyMap<string, number> = new Map([...SETUP_ALPHABET].map((ch, i) => [ch, i]));

export type SetupCodeErrorCode = 'setup_code_invalid' | 'setup_code_wrong_role' | 'setup_code_unsupported';

/** Why a code was refused, finer than the server's error code — for the message. */
export type SetupCodeProblem =
  | 'empty'
  | 'short'
  | 'long'
  | 'character'
  | 'check'
  | 'version'
  | 'unknown_role'
  | 'gateway'
  | 'wrong_role';

export class SetupCodeError extends Error {
  /** The server's code for the same refusal (`setup_code_invalid`, …). */
  readonly code: SetupCodeErrorCode;
  readonly problem: SetupCodeProblem;

  constructor(code: SetupCodeErrorCode, problem: SetupCodeProblem, message: string) {
    super(message);
    this.name = 'SetupCodeError';
    this.code = code;
    this.problem = problem;
  }
}

export interface ParsedSetupCode {
  role: SetupRole;
  /** The device's short id: 8 upper-case hex digits (u32, as the label's payload carries it). */
  shortId: string;
  /** The 25 symbols, upper case, no prefix, spaces or dashes. */
  normalized: string;
}

function gfMul(a: number, b: number): number {
  let out = 0;
  let x = a;
  let y = b;
  while (y) {
    if (y & 1) out ^= x;
    y >>= 1;
    x <<= 1;
    if (x & 0x20) x ^= GF32_MODULUS;
  }
  return out;
}

function checkValue(values: readonly number[]): number {
  let check = 0;
  let weight = 1;
  for (const v of values) {
    weight = gfMul(weight, 2);
    check ^= gfMul(weight, v);
  }
  return check;
}

/** Upper-case; strip the QR prefix, spaces and dashes; apply the Crockford aliases. */
export function normalizeSetupCode(text: string): string {
  let raw = (text ?? '').trim();
  if (raw.toUpperCase().startsWith(SETUP_QR_PREFIX)) raw = raw.slice(SETUP_QR_PREFIX.length);
  let out = '';
  for (const ch of raw.toUpperCase()) {
    if (SKIPPED.has(ch)) continue;
    out += ALIASES[ch] ?? ch;
  }
  return out;
}

/** 25 symbols in five groups of five: `4D6KR-ART00-0G40R-40M30-E2097`. */
export function groupSetupCode(symbols: string): string {
  const groups: string[] = [];
  for (let i = 0; i < symbols.length; i += 5) groups.push(symbols.slice(i, i + 5));
  return groups.join('-');
}

/** The code of a 15-byte payload (as the factory prints it). For tests and fixtures. */
export function encodeSetupPayload(payload: Uint8Array, grouped = true): string {
  if (payload.length !== SETUP_PAYLOAD_LENGTH) {
    throw new SetupCodeError('setup_code_invalid', 'long', 'The setup payload has the wrong length.');
  }
  const values: number[] = [];
  let acc = 0;
  let bits = 0;
  for (const byte of payload) {
    acc = ((acc << 8) | byte) & 0xffff;
    bits += 8;
    while (bits >= 5) {
      bits -= 5;
      values.push((acc >> bits) & 31);
    }
  }
  const text = values.map((v) => SETUP_ALPHABET[v]).join('') + SETUP_ALPHABET[checkValue(values)];
  return grouped ? groupSetupCode(text) : text;
}

function roleName(id: number): string {
  return id === 1 ? 'gateway' : id === 2 ? 'bridge' : id === 3 ? 'tag' : 'device';
}

/**
 * Parse a typed code or the QR text. Refuses what codes.py refuses, with the
 * same server codes: a wrong length, a character labels never use (`U`
 * included), a failed check symbol, a label for another protocol version, a
 * gateway or unknown role, and a label for the other role than `role`.
 */
export function parseSetupCode(text: string, role?: SetupRole): ParsedSetupCode {
  const symbols = normalizeSetupCode(text);
  if (!symbols) {
    throw new SetupCodeError('setup_code_invalid', 'empty', 'Enter the setup code from the label.');
  }
  const bad = [...symbols].find((ch) => !VALUE_OF.has(ch));
  if (bad !== undefined) {
    throw new SetupCodeError('setup_code_invalid', 'character',
      `The code has a character labels never use (${bad}). Check the label again.`);
  }
  if (symbols.length < SETUP_CODE_LENGTH) {
    throw new SetupCodeError('setup_code_invalid', 'short',
      `A setup code has ${SETUP_CODE_LENGTH} characters; this one has ${symbols.length}.`);
  }
  if (symbols.length > SETUP_CODE_LENGTH) {
    throw new SetupCodeError('setup_code_invalid', 'long',
      `A setup code has ${SETUP_CODE_LENGTH} characters; this one has ${symbols.length}.`);
  }
  const values = [...symbols].map((ch) => VALUE_OF.get(ch) as number);
  const data = values.slice(0, DATA_SYMBOLS);
  if (checkValue(data) !== values[DATA_SYMBOLS]) {
    throw new SetupCodeError('setup_code_invalid', 'check',
      'This code does not check out. Look for a typo, or scan the QR code instead.');
  }
  // 24 symbols × 5 bits = the 15 payload bytes, big-endian.
  const payload = new Uint8Array(SETUP_PAYLOAD_LENGTH);
  let acc = 0;
  let bits = 0;
  let at = 0;
  for (const v of data) {
    acc = ((acc << 5) | v) & 0xffff;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      payload[at++] = (acc >> bits) & 0xff;
    }
  }
  const head = payload[0];
  if ((head & 0xf0) !== VERSION_NIBBLE) {
    throw new SetupCodeError('setup_code_unsupported', 'version',
      'This label is for another kind of setup. Use the label that came with the device.');
  }
  const roleId = head & 0x0f;
  if (roleId === 1) {
    throw new SetupCodeError('setup_code_invalid', 'gateway', 'Gateways have no setup code.');
  }
  if (roleId !== SETUP_ROLE_IDS.bridge && roleId !== SETUP_ROLE_IDS.tag) {
    throw new SetupCodeError('setup_code_invalid', 'unknown_role', 'The label names an unknown kind of device.');
  }
  const parsedRole: SetupRole = roleId === SETUP_ROLE_IDS.bridge ? 'bridge' : 'tag';
  if (role && parsedRole !== role) {
    throw new SetupCodeError('setup_code_wrong_role', 'wrong_role',
      `This is a ${roleName(roleId)} label, not a ${role} label.`);
  }
  // short_id is little-endian in the payload; shown as the u32 in hex.
  const shortId = (payload[1] | (payload[2] << 8) | (payload[3] << 16) | (payload[4] << 24)) >>> 0;
  return {
    role: parsedRole,
    shortId: shortId.toString(16).toUpperCase().padStart(8, '0'),
    normalized: symbols,
  };
}

export type SetupCodeCheck =
  | { ok: true; value: ParsedSetupCode }
  | { ok: false; error: SetupCodeError };

/** `parseSetupCode` without the throw, for live validation. */
export function checkSetupCode(text: string, role?: SetupRole): SetupCodeCheck {
  try {
    return { ok: true, value: parseSetupCode(text, role) };
  } catch (e) {
    if (e instanceof SetupCodeError) return { ok: false, error: e };
    throw e;
  }
}

/**
 * Re-group what someone is typing or pasting: while it holds only code
 * characters (plus spaces, dashes or the QR prefix), show it in upper case in
 * groups of five. Text with a character labels never use is left exactly as
 * typed, so the message about it matches what is on screen.
 */
export function formatTypedSetupCode(text: string): string {
  const raw = text ?? '';
  const symbols = normalizeSetupCode(raw);
  if ([...symbols].some((ch) => !VALUE_OF.has(ch))) return raw;
  return groupSetupCode(symbols);
}
