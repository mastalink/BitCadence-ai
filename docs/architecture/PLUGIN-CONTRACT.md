# Runtime plugin contract v1

A plugin is an external program the local runtime launches under its own process supervision. The runtime scans one
directory level below ~/.mco/plugins for bitcadence-plugin.toml. The manifest does not load code into BitCadence or
register an OS task. See [the example manifest](../../plugins/example-hello/bitcadence-plugin.toml) and the
[generated JSON Schemas](../../schemas/plugins).

## Configure and approve

1. Copy the example directory to the deployment's plugin directory. Run
   **bitcadence plugin validate ~/.mco/plugins/example-hello** from the runtime user's environment.
2. Run the same command with **--lock-entry** and review all absolute paths, manifest and file hashes, and the
   files themselves. Install the reviewed plugin section in **~/.mco/plugins.lock.toml**, with a single
   top-level **version = 1**. Keep this deployment-specific lock private. Generate a fresh entry after changing
   an executable, script or manifest. The CLI never edits the lock or runs plugin commands.
3. Let the runtime discover the plugin. The authenticated **GET /v1/plugins** shows its trust state, changes on
   review, status, views, action definitions and a bounded list of recent structured events.

Each command declares an argv array. Executables resolve relative to the manifest directory or through the runtime's
PATH. Windows requires a native .exe. List scripts directly after recognized interpreters. The runtime pins
existing file arguments and recognized script extensions. Use **script_args** with zero-based argv indexes for
additional file arguments, including files passed as **--option=path**. Use paths that resolve at validation time.
Keep dependencies installed and controlled by the runtime user. The lock covers the manifest and resolved argv
files, so reviewing a plugin also includes its installed dependencies and behavior. Plugins run as the signed-in
OS user and can access that user's files. Install only code the owner trusts.

## Execution and status

- **processes** are long-lived commands with restart backoff and crash-loop cooldown.
- **schedules** declare interval_seconds and optional timeout_seconds. The initial fire is one interval in the
  future. SQLite WAL stores each schedule's next fire. A missed run catches up once and the next run keeps its
  original phase. A due run is skipped while the previous invocation is active. A crash after advancing the cursor
  and before process creation can skip that firing. The runtime never retries a claimed firing.
- **status** and **health** are interval commands. Each successful command returns one bitcadence.status/1 JSON
  document on stdout. **secrets** may report presence and expiry for declared names only. Never print credential
  values or secret-bearing free text. Runtime-known credentials are filtered, unknown credential values cannot be
  inferred, and child stderr is discarded. No client credentials are passed in the child environment.
- Lines starting with **@@bcd** and a space can carry an event JSON object with **type**, **state** and optional
  integer **progress** from 0 to 100. The runtime retains the latest 32 validated events per plugin. Command output
  is capped at 256 KiB.
- **views** carry fixed component descriptions and data references. **resources** are advisory.
- Existing global STOP and maintenance prevent plugin execution. A file at
  **~/.mco/plugin-pauses/plugins/PLUGIN** pauses that plugin. A file at
  **~/.mco/plugin-pauses/channels/CHANNEL** pauses commands declaring that channel. The runtime checks before
  spawning and each tick, terminates affected children and records an interrupted action as refused. A completed
  external effect still needs reconciliation.

## Owner actions

Install **~/.mco/plugin-owner.toml** with the owner's **instance** and the native confirmation issuer's Ed25519
**public_key** as 64 lowercase hex characters. Keep the signing key with the trusted native confirmation issuer.
This package supplies the runtime verifier and API boundary. It does not provide a native confirmation UI or
grant-minting endpoint.

Read the runtime audience and plugin trust fingerprint from **GET /v1/plugins**. Submit an authenticated
**POST /v1/plugins/PLUGIN/actions/ACTION** with **parameters** and **grant**. The grant contains a **claims** object
and a hex-encoded **signature** over the ASCII prefix **bitcadence.owner-grant/1** followed by a newline and then
the claims encoded as sorted-key ASCII JSON without insignificant whitespace. See **Grant** and **grant_message**
in the verifier for the exact encoding. Claims bind the owner, runtime audience, trust fingerprint, plugin,
action, exact typed parameters, random 128-bit nonce, verification method, and numeric UTC epoch issue and expiry
times. Grants last at most 60 seconds and can be used once. The trusted issuer attests native confirmation.
Actions marked **dangerous** require the issuer to attest **platform-user-verified**.

The runtime appends one argv element for each typed parameter in declaration order. The action program must
interpret them as data and should use its own explicit option terminator if appropriate. The authenticated
**GET /v1/plugins/actions/RUN** returns the run outcome. The persistent hash-chained audit stores identity, action,
time and outcome. It omits parameter values, signatures and subprocess output.
