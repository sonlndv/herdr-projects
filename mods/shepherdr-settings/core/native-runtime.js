// Native engines own execution. This bridge only addresses an existing session on its machine.
export const NATIVE_KINDS = Object.freeze(["omp", "omo", "omh", "hermes"]);
const NAMES = Object.freeze({ omp: "OMP", omo: "OMO", omh: "Hermes / OMH", hermes: "Hermes" });
export const isNative = (kind) => NATIVE_KINDS.includes(kind);
export const runtimeName = (kind) => NAMES[kind] ?? kind ?? "Agent";

// The same helper is installed with the desktop mods and the web-only service. The override is
// for a source checkout; it names a program, never a different Herdr home or core session.
export const NATIVE = `
runtime="$1"; operation="$2"; body="$3"
[ -n "$body" ] || body='{}'
helper=""
if [ -n "$SHEPHERDR_NATIVE_HELPER" ] && [ -f "$SHEPHERDR_NATIVE_HELPER" ]; then
  helper="$SHEPHERDR_NATIVE_HELPER"
else
  for file in "\${HERDR_APP_HOME:-$HOME/.config/herdr-app}/bin/shepherdr-native" "$HOME/.local/share/shepherdr/bin/shepherdr-native"; do
    if [ -f "$file" ]; then helper="$file"; break; fi
  done
fi
if [ -z "$helper" ]; then
  printf '%s\\n' '{"error":"The native runtime helper is not installed on this machine; update this Shepherdr installation."}'
  exit 1
fi
exec python3 "$helper" "$runtime" "$operation" "$body"
`;

export function nativeTarget(terminal, reader = null) {
  const out = {};
  const pid = terminal?.foreground?.pid ?? reader?.pid;
  if (Number.isSafeInteger(pid) && pid > 0) out.pid = pid;
  const state = reader?.nativeState ?? reader?.native;
  if (typeof state?.sessionId === "string" && state.sessionId) out.sessionId = state.sessionId;
  if (typeof state?.sessionFile === "string" && state.sessionFile) out.sessionFile = state.sessionFile;
  const cwd = terminal?.foreground?.cwd ?? terminal?.cwd;
  if (cwd) out.cwd = cwd;
  if (terminal?.address) out.terminal = terminal.address.split("/").pop();
  return out;
}

/** @param {string | null} machine */
export async function nativeRequest(io, kind, operation, payload = {}, machine = null) {
  if (!isNative(kind)) throw new Error(`Unknown native runtime: ${kind}`);
  if (!io || typeof io.sh !== "function") throw new Error("The native runtime transport is unavailable.");
  const response = await io.sh(NATIVE, ["shepherdr-native", kind, operation, JSON.stringify(payload)], machine);
  let result;
  try { result = JSON.parse(String(response?.stdout ?? "")); }
  catch { throw new Error(`${runtimeName(kind)} returned an invalid native response.`); }
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error(`${runtimeName(kind)} returned an invalid native result.`);
  if (response?.code !== 0 || result.error) throw new Error(String(result.error || response?.stderr || `${runtimeName(kind)} rejected ${operation}.`));
  return result;
}

export const nativeQuestionId = (sessionId, requestId) =>
  `native:${encodeURIComponent(sessionId)}:${encodeURIComponent(requestId)}`;
