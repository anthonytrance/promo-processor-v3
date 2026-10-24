# Standing rules for every code change

Every agent working on this skill reads these at the start of a session and checks them before finishing a change.

1. A new Python package, external program or sibling skill goes into `requirements.txt` or `dependencies.json` in the same change, with its version.
2. No hard-coded user paths, account names or folder names. Add a key to the settings file (see Setup in SKILL.md) with a neutral default, or use `sys.executable` for "the Python running this".
3. No secrets, tokens, passwords, protected promo URLs, mail contents or the inventory database in the skill folder. Runtime state lives in the state folder named by the settings file. The pre-commit secret check enforces this; never bypass it.
4. Tests never touch real accounts: stub `store.sync_logs` and every network client, and keep fixtures to example names and addresses.
5. Saved logins are encrypted with Windows DPAPI or kept in the Windows credential store, per Windows user. The skill is Windows only by design.
