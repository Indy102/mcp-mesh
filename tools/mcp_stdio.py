"""The stdio dispatch for the SUPPORTED PROFILE of MCP 2026-07-28, shared by every
server in this mesh (the leaves and the client-facing gateway).

Dispatch is written directly rather than delegated to the SDK server, because the
profile requires exact control of the JSON-RPC error ladder — parse failures, invalid
requests, version negotiation and capability errors are decided before any handler
runs, and the SDK does not expose those decision points. The SDK is still used as an
independent check that our published schemas load in its `Tool` model (T1 §9).

A server DECLARES the tools it implements. Rev 10 advertised the whole specified
catalogue from every leaf, so `weekly_report` appeared in `tools/list` and then answered
`insufficient_evidence` because no leaf implements it. A tool that is specified but
served by nobody is not advertised and is not known: it answers -32602 Unknown tool,
like any other unknown name.

stdout carries ONLY protocol frames. Every diagnostic goes to stderr.
"""
import sys, json, uuid, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import contracts as C
from jsonschema import Draft202012Validator as V

PV = C.PROTOCOL_VERSION
SUPPORTED_VERSIONS = [PV]
METHODS = {"server/discover", "tools/list", "tools/call"}

# ---- STATED input limits. "Survives every malformed input" is not a claim anyone can
# make; these are the bounds these servers support, enforced before parsing.
MAX_FRAME_BYTES = 1 << 20        # 1 MiB per line
MAX_NESTING_DEPTH = 100          # json.loads recurses; deeper frames are refused
FC = __import__("jsonschema").FormatChecker()


class Server:
    """Everything that differs between the leaf and the gateway.

    implemented   the tool names this server ACTUALLY serves; the catalogue it
                  advertises and the only names it accepts.
    call          call(name, args) -> CallToolResult body
    """
    def __init__(self, *, name, version, instructions, implemented, call,
                 requires_capability=None, test_only_tools=None,
                 test_only_capability=None):
        self.info = {"name": name, "version": version}
        self.instructions = instructions
        self.implemented = tuple(implemented)
        self.call = call
        self.requires_capability = dict(requires_capability or {})
        self.test_only_tools = dict(test_only_tools or {})
        self.test_only_capability = dict(test_only_capability or {})
        unknown = [t for t in self.implemented if t not in C.INPUTS]
        if unknown:
            raise ValueError(f"implemented tools not in the contract: {unknown}")

    def knows(self, name):
        return name in self.implemented or name in self.test_only_tools


def _is_valid_id(v):
    """Official RequestId is string | integer. bool is an int subclass in Python but is
    NOT a valid JSON-RPC id, and a fractional number is not an integer."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, str)


def _err(rid, code, message, data=None, include_id=True):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    out = {"jsonrpc": "2.0", "error": e}
    if include_id:
        out["id"] = rid
    return out


def _ok(rid, result, srv):
    result = dict(result)
    result["resultType"] = "complete"          # emitted by the server, never injected later
    result.setdefault("_meta", {})["io.modelcontextprotocol/serverInfo"] = srv.info
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def discover_result(srv):
    return {"supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}, "extensions": {}},
            "instructions": srv.instructions,
            "ttlMs": 300000, "cacheScope": "private"}


def list_tools_result(srv):
    """Exactly the tools this server implements, in deterministic order."""
    tools = []
    for name in sorted(srv.implemented):
        tools.append({"name": name, "title": name.replace("_", " ").title(),
                      "description": f"MCP Mesh {name}",
                      "inputSchema": C.input_schema(name),
                      "outputSchema": C.output_schema(name)})
    return {"tools": tools, "ttlMs": 300000, "cacheScope": "private"}


def handle(msg, srv):
    """Every field's TYPE is checked before it is used.

    Order, and why:
      1. envelope shape and types      -> -32600 (id unreadable)
      2. id presence and type          -> -32600 (an invalid id cannot be echoed)
      3. params / _meta types          -> -32602
      4. required _meta fields         -> -32602 (absent is different from invalid)
      5. protocol version SUPPORT      -> -32022 via negotiation, never structural
      6. method membership             -> -32601
      7. tool existence                -> -32602
      8. ARGUMENTS                     -> -32602   (declared to precede capability)
      9. capability                    -> -32021
    """
    # 1. envelope
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "Invalid Request: jsonrpc must be \"2.0\"",
                    include_id=False)
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        return _err(None, -32600, "Invalid Request: method must be a non-empty string",
                    include_id=False)

    # 2. id: ABSENT means notification; PRESENT-but-invalid is an invalid request.
    has_id = "id" in msg
    rid = msg.get("id")
    if not has_id:
        return None                                    # notification: no response
    if rid is None:
        return _err(None, -32600,
                    "Invalid Request: id must not be null (JSON-RPC 2.0)",
                    include_id=False)
    if not _is_valid_id(rid):
        return _err(None, -32600,
                    f"Invalid Request: id must be a string or integer, got "
                    f"{type(rid).__name__}", include_id=False)

    # 3. params / _meta types. An ABSENT params defaults; an explicit null does not.
    if "params" in msg and msg["params"] is None:
        return _err(rid, -32602, "Invalid params: params must not be null")
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return _err(rid, -32602,
                    f"Invalid params: must be an object, got {type(params).__name__}")
    meta = params.get("_meta", {})
    if not isinstance(meta, dict):
        return _err(rid, -32602,
                    f"Invalid params: _meta must be an object, got {type(meta).__name__}")

    # 4. required _meta fields: absent vs invalid are distinct messages
    if "io.modelcontextprotocol/protocolVersion" not in meta:
        return _err(rid, -32602, "Invalid params: _meta is missing protocolVersion")
    ver = meta["io.modelcontextprotocol/protocolVersion"]
    if not isinstance(ver, str):
        return _err(rid, -32602,
                    f"Invalid params: protocolVersion must be a string, got "
                    f"{type(ver).__name__}")
    if "io.modelcontextprotocol/clientCapabilities" not in meta:
        return _err(rid, -32602, "Invalid params: _meta is missing clientCapabilities")
    caps = meta["io.modelcontextprotocol/clientCapabilities"]
    if not isinstance(caps, dict):
        return _err(rid, -32602,
                    f"Invalid params: clientCapabilities must be an object, got "
                    f"{type(caps).__name__}")

    # 5. version SUPPORT is negotiation, not structure
    if ver not in SUPPORTED_VERSIONS:
        return _err(rid, -32022, "Unsupported protocol version",
                    {"supported": list(SUPPORTED_VERSIONS), "requested": ver})

    # 6. method
    if method not in METHODS:
        return _err(rid, -32601, f"Method not found: {method}")
    if method == "server/discover":
        return _ok(rid, discover_result(srv), srv)
    if method == "tools/list":
        return _ok(rid, list_tools_result(srv), srv)

    # 7. tool. A specified-but-unimplemented tool is UNKNOWN to this server.
    name = params.get("name")
    if not isinstance(name, str):
        return _err(rid, -32602,
                    "Invalid params: tools/call requires a string tool name")
    if not srv.knows(name):
        return _err(rid, -32602, f"Unknown tool: {name}")

    # 8. ARGUMENTS before capability, as declared
    # Absence and explicit null are different: `arguments: null` is not `{}`.
    if "arguments" in params and params["arguments"] is None:
        return _err(rid, -32602, "Invalid params: arguments must not be null")
    args = params.get("arguments", {})
    if not isinstance(args, dict):
        return _err(rid, -32602,
                    f"Invalid params: arguments must be an object, got "
                    f"{type(args).__name__}")
    if name in C.INPUTS:
        # A format checker is required, or "2026-02-31" reaches tool execution.
        errs = list(V(C.input_schema(name), format_checker=FC).iter_errors(args))
        if errs:
            return _err(rid, -32602, f"Invalid arguments for {name}: {errs[0].message}")
        sem = C.semantic_errors(name, args)
        if sem:
            return _err(rid, -32602, f"Invalid arguments for {name}: {sem}")

    # 9. capability, last
    need = srv.requires_capability.get(name) or srv.test_only_capability.get(name)
    if need and need not in caps:
        return _err(rid, -32021, "Required client capability not declared",
                    {"requiredCapabilities": [need]})

    try:
        return _ok(rid, srv.call(name, args), srv)
    except Exception:
        incident = uuid.uuid4().hex[:16]
        print(f"internal fault incident={incident}", file=sys.stderr, flush=True)
        return _err(rid, -32603, "Internal error", {"incident_id": incident})


def _max_depth(s, limit):
    """Cheap bracket-depth scan performed BEFORE json.loads, so a deeply nested frame
    is refused rather than raising RecursionError inside the parser."""
    depth = mx = 0
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > mx:
                mx = depth
                if mx > limit:
                    return mx
        elif ch in "]}":
            depth -= 1
    return mx


def main(srv):
    print(f"mesh stdio server ready: {srv.info['name']}", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
            resp = _err(None, -32600,
                        f"Invalid Request: frame exceeds {MAX_FRAME_BYTES} bytes",
                        include_id=False)
            sys.stdout.write(json.dumps(resp) + "\n"); sys.stdout.flush(); continue
        if _max_depth(line, MAX_NESTING_DEPTH) > MAX_NESTING_DEPTH:
            resp = _err(None, -32600,
                        f"Invalid Request: nesting exceeds {MAX_NESTING_DEPTH}",
                        include_id=False)
            sys.stdout.write(json.dumps(resp) + "\n"); sys.stdout.flush(); continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError):
            resp = _err(None, -32700, "Parse error", include_id=False)
        else:
            # The ENTIRE dispatch is guarded: a malformed frame must never terminate
            # the server, or the next valid request receives no response.
            try:
                resp = handle(msg, srv)
            except Exception:
                incident = uuid.uuid4().hex[:16]
                print(f"dispatch fault incident={incident}", file=sys.stderr, flush=True)
                rid = msg.get("id") if isinstance(msg, dict) else None
                resp = _err(rid if _is_valid_id(rid) else None, -32603, "Internal error",
                            {"incident_id": incident},
                            include_id=_is_valid_id(rid))
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def payload_result(payload, is_error=False):
    """One CallToolResult body from one payload, serialized once."""
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, sort_keys=True,
                                            separators=(",", ":"))}],
            "structuredContent": payload, "isError": is_error}


def error_payload(error_type, message, remediation, retryable, details=None):
    p = {"schema_version": C.SCHEMA_VERSION, "payload_type": "error",
         "error_type": error_type, "message": message,
         "remediation": remediation, "retryable": retryable}
    if details is not None:
        p["details"] = details
    return p
