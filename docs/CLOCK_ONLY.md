# Scheduling architecture

Docket schedules time-based wakes and bounded watches. It can also publish optional event notices; see [Tower notices](TOWER.md).

It does not accept event subscriptions, execute sandboxed workloads or run shell commands. A wake is a message to an existing compatible session. Startup preserves any legacy event tables and private configuration without draining or replaying them.
