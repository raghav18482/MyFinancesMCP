// Shared store for the user's OpenRouter API key.
//
// The key is encrypted at rest in localStorage with the Angel One client id as
// the passphrase, so it is never held in plaintext and never sent anywhere but
// our own API. AES-GCM is used wherever crypto.subtle exists (HTTPS and
// localhost); plain HTTP falls back to a XOR obfuscation, which is not real
// encryption but keeps the key out of a casual localStorage glance.
//
// Both the dashboard and the research page read the same entry, so saving the
// key once on /dashboard unlocks the AI summary on /research.

const LS_KEY = 'mfmcp_ai_key';
const MODEL_LS_KEY = 'mfmcp_ai_model';
const PRESET_MODELS = [
  'openai/gpt-4o-mini',
  'openai/gpt-4o',
  'anthropic/claude-3.5-haiku',
  'google/gemini-2.0-flash-001',
];
const HAS_SUBTLE = !!(window.crypto && window.crypto.subtle);

// ── Byte helpers ──
function strToBytes(s) { return new TextEncoder().encode(s); }
function bytesToStr(buf) { return new TextDecoder().decode(buf); }

function b64Encode(buf) {
  const bytes = new Uint8Array(buf);
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function b64Decode(s) {
  const binary = atob(s);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// ── AES-GCM path (secure contexts: HTTPS / localhost) ──
async function aesEncrypt(plaintext, clientId) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const raw = await crypto.subtle.importKey('raw', strToBytes(clientId), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    raw, { name: 'AES-GCM', length: 256 }, false, ['encrypt'],
  );
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, strToBytes(plaintext));
  return JSON.stringify({ v: 2, s: b64Encode(salt), i: b64Encode(iv), c: b64Encode(ct) });
}

async function aesDecrypt(stored, clientId) {
  const { s, i, c } = JSON.parse(stored);
  const salt = b64Decode(s), iv = b64Decode(i), ct = b64Decode(c);
  const raw = await crypto.subtle.importKey('raw', strToBytes(clientId), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    raw, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, ct);
  return bytesToStr(pt);
}

// ── XOR fallback (non-secure HTTP contexts) ──
function xorWithKey(input, secret) {
  const out = new Uint8Array(input.length);
  for (let i = 0; i < input.length; i++) out[i] = input[i] ^ secret.charCodeAt(i % secret.length);
  return out;
}

function xorEncrypt(plaintext, clientId) {
  return JSON.stringify({ v: 1, d: b64Encode(xorWithKey(strToBytes(plaintext), clientId)) });
}

function xorDecrypt(stored, clientId) {
  const { d } = JSON.parse(stored);
  return bytesToStr(xorWithKey(b64Decode(d), clientId));
}

// ── Unified encrypt / decrypt ──
async function encryptKey(plaintext, clientId) {
  return HAS_SUBTLE ? aesEncrypt(plaintext, clientId) : xorEncrypt(plaintext, clientId);
}

async function decryptKey(clientId) {
  const raw = localStorage.getItem(LS_KEY);
  if (!raw) return null;
  try {
    const obj = JSON.parse(raw);
    if (obj.v === 2 && HAS_SUBTLE) return await aesDecrypt(raw, clientId);
    if (obj.v === 1) return xorDecrypt(raw, clientId);
    return null;
  } catch (e) {
    console.warn('Decrypt failed', e);
    return null;
  }
}

function hasStoredKey() { return !!localStorage.getItem(LS_KEY); }
function clearStoredKey() { localStorage.removeItem(LS_KEY); }

async function saveKey(plaintext, clientId) {
  localStorage.setItem(LS_KEY, await encryptKey(plaintext, clientId));
}

/** The model the user last picked on the dashboard, or the default. */
function storedModel() {
  return localStorage.getItem(MODEL_LS_KEY) || PRESET_MODELS[0];
}

function storeModel(model) {
  localStorage.setItem(MODEL_LS_KEY, model);
}

/**
 * POST to one of our AI endpoints with the decrypted key and chosen model
 * attached. Resolves to the parsed JSON, or `{error}` if no key is usable.
 */
async function callAI(endpoint, body, clientId) {
  const apiKey = await decryptKey(clientId);
  if (!apiKey) {
    return { error: 'Could not decrypt your API key. Please re-save it on the Dashboard.' };
  }
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, api_key: apiKey, model: storedModel() }),
  });
  return res.json();
}

/**
 * Render LLM prose safely: escape every tag, then re-apply **bold** and turn
 * newlines into breaks. Never assign model output to innerHTML unescaped.
 */
function renderAiText(text) {
  const escaped = String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return escaped
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br>');
}

export {
  LS_KEY,
  MODEL_LS_KEY,
  PRESET_MODELS,
  HAS_SUBTLE,
  encryptKey,
  decryptKey,
  saveKey,
  hasStoredKey,
  clearStoredKey,
  storedModel,
  storeModel,
  callAI,
  renderAiText,
};
