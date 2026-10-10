# Security and data safety

Titan monitors the local system as the current user. It never requests silent
elevation, collects credentials, changes security settings, uploads data or
modifies storage. Optional SMART queries read attributes only.

Process termination is explicitly confirmed, identity-checked by PID plus start
time, and refused for known critical, ancestor and identifiable service processes.
It requests termination once and never escalates to a forced kill. Operating
system access restrictions are respected. Unsaved work can still be lost.

Reports redact sensitive identifiers by default, but review exported files before
sharing them: unusual vendor strings can contain unexpected personal data.
Detailed exports require an explicit review flow. Local configuration, history
and logs follow operating system permissions; Titan does not provide encryption.

For a suspected vulnerability, do not publish sensitive system dumps. Contact the
repository maintainers privately through the hosting platform's private security
reporting mechanism when available. Include a minimal reproduction with synthetic
identifiers, the Titan version and operating system. This source delivery does
not yet have a public repository or security inbox.
