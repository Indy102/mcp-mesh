# Third-party materials

`schemas/mcp-official-2026-07-28.json` is vendored from the Model Context Protocol repository at commit `271ecc9accafdd9b83a3c869fa67c22953b2af80`. Its existing source URL and SHA-256 remain in `schemas/mcp-official-2026-07-28.provenance.json`.

The [upstream license file](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/271ecc9accafdd9b83a3c869fa67c22953b2af80/LICENSE) is included unchanged in [third-party/mcp-schema-LICENSE](third-party/mcp-schema-LICENSE), retrieved from the same pinned commit. License file SHA-256: `0382b0057770ca05e9c350a50aa3b1c1fea84da0bc81d723bf00b9aa841be58a`.

The upstream file describes an MIT-to-Apache-2.0 transition, retains MIT terms for contributions not relicensed, and separately addresses non-specification documentation. The complete transition notice and license texts are preserved; this project does not infer a single blanket license for the schema from that mixed notice.

Python dependencies remain pinned by `uv.lock` and retain their respective upstream licenses. Including those upstream notices does not assign a new license to the project's own code.

The verification workflow uses immutable revisions of [actions/checkout](https://github.com/actions/checkout/tree/d23441a48e516b6c34aea4fa41551a30e30af803) and [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv/tree/20cfd1bf945f4377ade1205e4dbc17946fc9a30d). These actions are referenced, not vendored.
