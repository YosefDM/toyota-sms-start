/*
 * Template for the HTTPToolkit unpinning config. Copy to config.js and fill CERT_PEM.
 * ⚠️ NEVER commit the real config.js — CERT_PEM is your TLS-filter's CA (home-network specific).
 * Only needed when the rig runs behind a TLS-intercepting filter (a TLS-intercepting content filter). On an unfiltered
 * network, skip unpinning entirely and load only toyota_bypass.js.
 *
 * Build the combined script Frida actually loads (Frida gives each -l its own scope, so these MUST
 * be concatenated):
 *   cat config.js android-certificate-unpinning.js > combined_unpin.js
 *
 * CRITICAL: CERT_PEM must start EXACTLY with the BEGIN line on line 0 — no leading newline, or the
 * script's pemToDer throws "certificate should be in PEM format". Use the filter's CA, pure PEM.
 */
const CERT_PEM = `-----BEGIN CERTIFICATE-----
[!! PASTE YOUR TLS FILTER'S CA certificate data HERE — pure PEM, no leading newline !!]
-----END CERTIFICATE-----`;

const PROXY_HOST = '127.0.0.1';   // unused in our flow (we don't proxy; we just trust the filter's CA)
const PROXY_PORT = 8000;
const DEBUG_MODE = true;          // verbose Frida output while bringing it up; set false in production
