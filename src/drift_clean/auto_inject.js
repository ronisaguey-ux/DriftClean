/**
 * auto-inject entrypoint for Node.js runtimes — DISABLED 2026-09-10 by owner order.
 *
 * This file used to call driftClean({silent:true}) on every Node process start,
 * because NODE_OPTIONS="--require <this file>" is exported from ~/.bashrc and
 * ~/.profile. That meant any node process — opencode, an MCP server, a test
 * harness — silently rewrote session transcripts. Cleaning is /clean only now.
 *
 * The real implementation is kept next to this file as auto_inject.js.disabled-*.
 * Do not restore it without the owner asking.
 */
