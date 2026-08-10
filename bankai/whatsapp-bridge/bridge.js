/**
 * BankAI WhatsApp bridge — the copilot's own seat in the household group chat.
 *
 * A thin Baileys (WhatsApp multi-device protocol) sidecar with exactly one job:
 * move messages between WhatsApp and the filesystem. All judgment — who is
 * household, what is an expense, whether to reply — lives in the Python app.
 *
 *   inbound:  every group message  -> append JSON line to DATA_DIR/inbound.jsonl
 *   outbound: DATA_DIR/outbox/*.json ({to, text}) -> sent, then moved to sent/
 *   status:   DATA_DIR/status.json heartbeat (connection, own JID, groups seen,
 *             pairing QR/code) so the Python side can report bridge health.
 *
 * The session in DATA_DIR/session/ is the copilot's OWN WhatsApp account on its
 * own number — deliberately not a person's account: unofficial clients carry a
 * ban risk, and the blast radius must be a dedicated number, not a spouse's.
 *
 * Pairing (one-time, human does this):
 *   - QR: watch the service log, scan from the copilot phone's WhatsApp
 *     (Settings > Linked Devices > Link a Device).
 *   - Or set WHATSAPP_PAIRING_NUMBER=1XXXXXXXXXX to get an 8-char code to type
 *     into the phone instead; the code also lands in status.json.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
} = require("@whiskeysockets/baileys");
const qrcodeTerminal = require("qrcode-terminal");
const pino = require("pino");

const DATA_DIR = process.env.WHATSAPP_DATA_DIR || "/root/bankai-data/whatsapp";
const SESSION_DIR = path.join(DATA_DIR, "session");
const INBOUND_FILE = path.join(DATA_DIR, "inbound.jsonl");
const OUTBOX_DIR = path.join(DATA_DIR, "outbox");
const SENT_DIR = path.join(DATA_DIR, "sent");
const STATUS_FILE = path.join(DATA_DIR, "status.json");
const PAIRING_NUMBER = (process.env.WHATSAPP_PAIRING_NUMBER || "").replace(/\D/g, "");

for (const dir of [DATA_DIR, SESSION_DIR, OUTBOX_DIR, SENT_DIR]) {
  fs.mkdirSync(dir, { recursive: true });
}

const log = (...args) => console.log(new Date().toISOString(), ...args);

const status = {
  connected: false,
  me: null,
  groups: {}, // jid -> subject, refreshed on connect and on group metadata events
  pairing_code: null,
  qr: null,
  updated_at: null,
};

function writeStatus() {
  status.updated_at = new Date().toISOString();
  fs.writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2));
}

/** The text a human actually typed, wherever WhatsApp nests it. */
function extractText(message) {
  if (!message) return "";
  if (message.conversation) return message.conversation;
  if (message.extendedTextMessage) return message.extendedTextMessage.text || "";
  if (message.imageMessage) return message.imageMessage.caption || "[image]";
  if (message.videoMessage) return message.videoMessage.caption || "[video]";
  if (message.documentMessage) {
    return `[document: ${message.documentMessage.fileName || "file"}]`;
  }
  if (message.audioMessage) return "[voice message]";
  if (message.ephemeralMessage) return extractText(message.ephemeralMessage.message);
  if (message.viewOnceMessage) return extractText(message.viewOnceMessage.message);
  return "";
}

/** Best-effort phone number from a JID; empty for privacy LIDs. */
function numberFromJid(jid) {
  if (!jid || !jid.endsWith("@s.whatsapp.net")) return "";
  return "+" + jid.split("@")[0].split(":")[0];
}

let sock = null;
let heartbeat = null;

async function refreshGroups() {
  try {
    const groups = await sock.groupFetchAllParticipating();
    status.groups = {};
    for (const [jid, meta] of Object.entries(groups)) {
      status.groups[jid] = meta.subject || "";
    }
    writeStatus();
    log("groups:", JSON.stringify(status.groups));
  } catch (err) {
    log("group refresh failed:", err.message);
  }
}

function recordInbound(msg) {
  const jid = msg.key.remoteJid || "";
  if (!jid.endsWith("@g.us")) return; // group seat only — no DMs in v1
  if (msg.key.fromMe) return;
  const text = extractText(msg.message);
  if (!text) return;
  const senderJid = msg.key.participant || "";
  const line = {
    id: msg.key.id,
    group_jid: jid,
    group_subject: status.groups[jid] || "",
    sender_jid: senderJid,
    // participantPn appears on lid-addressed messages in newer server payloads;
    // it is the phone number behind the LID when the server provides it.
    sender_number:
      numberFromJid(msg.key.participantPn || "") || numberFromJid(senderJid),
    push_name: msg.pushName || "",
    text,
    timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
  };
  fs.appendFileSync(INBOUND_FILE, JSON.stringify(line) + "\n");
  log(`inbound <- ${line.push_name || line.sender_jid} in ${line.group_subject || jid}: ${text.slice(0, 80)}`);
}

async function drainOutbox() {
  let names;
  try {
    names = fs.readdirSync(OUTBOX_DIR).filter((n) => n.endsWith(".json"));
  } catch {
    return;
  }
  for (const name of names.sort()) {
    const file = path.join(OUTBOX_DIR, name);
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(file, "utf8"));
    } catch (err) {
      log(`outbox ${name}: unreadable (${err.message}) — leaving in place`);
      continue;
    }
    if (!payload.to || !payload.text) {
      log(`outbox ${name}: missing to/text — moving to sent/ unsent`);
      fs.renameSync(file, path.join(SENT_DIR, name + ".invalid"));
      continue;
    }
    try {
      await sock.sendMessage(payload.to, { text: payload.text });
      fs.renameSync(file, path.join(SENT_DIR, name));
      log(`outbound -> ${payload.to}: ${payload.text.slice(0, 80)}`);
    } catch (err) {
      // leave the file: retried on the next drain once the socket recovers
      log(`outbox ${name}: send failed (${err.message}) — will retry`);
      return;
    }
  }
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();
  sock = makeWASocket({
    version,
    auth: state,
    logger: pino({ level: "warn" }),
    // The copilot reads what people say; it has no business auto-downloading
    // every photo in the family group.
    shouldSyncHistoryMessage: () => false,
    markOnlineOnConnect: false,
  });

  sock.ev.on("creds.update", saveCreds);

  if (PAIRING_NUMBER && !state.creds.registered) {
    // Pairing-code flow: friendlier than a QR over a headless box.
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(PAIRING_NUMBER);
        status.pairing_code = code;
        writeStatus();
        log(`PAIRING CODE for +${PAIRING_NUMBER}: ${code}`);
        log("On the copilot phone: WhatsApp > Linked Devices > Link a Device > Link with phone number");
      } catch (err) {
        log("pairing code request failed:", err.message);
      }
    }, 4000);
  }

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      status.qr = qr;
      writeStatus();
      if (!PAIRING_NUMBER) {
        log("Scan this QR from the copilot phone (WhatsApp > Linked Devices):");
        qrcodeTerminal.generate(qr, { small: true });
      }
    }
    if (connection === "open") {
      status.connected = true;
      status.qr = null;
      status.pairing_code = null;
      status.me = sock.user ? { id: sock.user.id, name: sock.user.name || "" } : null;
      writeStatus();
      log("connected as", JSON.stringify(status.me));
      refreshGroups();
    }
    if (connection === "close") {
      status.connected = false;
      writeStatus();
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        // Session revoked from the phone. Do NOT clear it ourselves — that is
        // a human call. Park and report.
        log("LOGGED OUT by the phone — re-pairing required. Bridge idling.");
        return;
      }
      log(`connection closed (${code}) — reconnecting in 5s`);
      setTimeout(start, 5000);
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return; // live messages only, never history sync
    for (const msg of messages) {
      try {
        recordInbound(msg);
      } catch (err) {
        log("inbound record failed:", err.message);
      }
    }
  });

  sock.ev.on("groups.upsert", refreshGroups);

  if (!heartbeat) {
    heartbeat = setInterval(() => {
      writeStatus();
      if (status.connected) drainOutbox();
    }, 2000);
  }
}

process.on("unhandledRejection", (err) => log("unhandled rejection:", err));
start().catch((err) => {
  log("fatal:", err);
  process.exit(1);
});
