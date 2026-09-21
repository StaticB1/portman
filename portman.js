#!/usr/bin/env node
'use strict';

const { execSync, spawnSync, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

// ── Config ────────────────────────────────────────────────────────────────────
const REGISTRY_FILE = path.join(os.homedir(), '.portman.json');
const BACKUP_FILE   = REGISTRY_FILE + '.bak';
const SCHEMA_VERSION = 1;

// Ports that belong to the operating system or a database, not to a dev server.
// `kill` and the auto-free in `start` refuse these without --force. Portman used
// to SIGTERM anything on any port, so `portman kill 22` would take out sshd.
const PROTECTED_PORTS = new Set([
  22, 25, 53, 67, 68, 111, 123, 139, 143, 389, 445, 465, 587, 631, 993, 995,
  1433, 3306, 5353, 5432, 6379, 9200, 11211, 27017,
]);

// The spine holds FLEET.json, the source of truth for dev ports. Derive the path
// where we can (this file normally lives at <spine>/tools/portman/) and fall back
// for the installed copy under ~/.local/share/portman.
function findSpine() {
  const candidates = [
    process.env.PORTMAN_SPINE,
    path.resolve(__dirname, '..', '..'),
    path.join(os.homedir(), 'projects'),
  ].filter(Boolean);
  for (const dir of candidates) {
    if (fs.existsSync(path.join(dir, 'FLEET.json'))) return dir;
  }
  return null;
}

// ── Colors ───────────────────────────────────────────────────────────────────
const c = {
  reset:  '\x1b[0m',
  bold:   '\x1b[1m',
  dim:    '\x1b[2m',
  red:    '\x1b[31m',
  green:  '\x1b[32m',
  yellow: '\x1b[33m',
  blue:   '\x1b[34m',
  cyan:   '\x1b[36m',
  white:  '\x1b[37m',
  gray:   '\x1b[90m',
};
const clr = (color, text) => `${c[color]}${text}${c.reset}`;
const bold = (text) => `${c.bold}${text}${c.reset}`;

// ── Registry I/O ──────────────────────────────────────────────────────────────
// A corrupt registry used to parse as `{projects:{}}`, so `list` printed "no
// projects registered" and the next `add` silently overwrote the whole file.
// Now: refuse to continue, and point at the backup.
function registryFatal(msg) {
  console.error(clr('red', `✘ Registry unreadable: ${msg}`));
  console.error(clr('gray', `  File: ${REGISTRY_FILE}`));
  if (fs.existsSync(BACKUP_FILE)) {
    console.error(clr('gray', `  A backup from the previous write exists:`));
    console.error(clr('gray', `    cp ${BACKUP_FILE} ${REGISTRY_FILE}`));
  }
  console.error(clr('gray', '  Refusing to continue — writing now would destroy the registry.'));
  process.exit(2);
}

function loadRegistry() {
  if (!fs.existsSync(REGISTRY_FILE)) return { schemaVersion: SCHEMA_VERSION, projects: {} };
  let raw;
  try {
    raw = fs.readFileSync(REGISTRY_FILE, 'utf8');
  } catch (e) {
    registryFatal(`cannot read it (${e.message})`);
  }
  if (!raw.trim()) return { schemaVersion: SCHEMA_VERSION, projects: {} };
  let reg;
  try {
    reg = JSON.parse(raw);
  } catch (e) {
    registryFatal(`not valid JSON (${e.message})`);
  }
  if (!reg || typeof reg !== 'object' || typeof reg.projects !== 'object' || reg.projects === null) {
    registryFatal('valid JSON but has no "projects" object');
  }
  return reg;
}

// Atomic: write a temp file, fsync, rename. A crash mid-write can no longer
// leave a half-written registry, and the previous good copy is kept as .bak.
function saveRegistry(reg) {
  reg.schemaVersion = SCHEMA_VERSION;
  const tmp = `${REGISTRY_FILE}.tmp.${process.pid}`;
  if (fs.existsSync(REGISTRY_FILE)) {
    try { fs.copyFileSync(REGISTRY_FILE, BACKUP_FILE); } catch {}
  }
  const fd = fs.openSync(tmp, 'w', 0o600);
  try {
    fs.writeSync(fd, JSON.stringify(reg, null, 2) + '\n');
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(tmp, REGISTRY_FILE);
}

// ── Port utilities ────────────────────────────────────────────────────────────
// Port state is a THREE-way answer: free, busy, or unknown. The old code folded
// "the query failed" into "nothing is listening", so with ss/lsof missing every
// port read as free — `check` said free while a server was up, and `start` would
// happily launch a second one.
function sleepMs(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch {
    spawnSync('sleep', [String(ms / 1000)]);
  }
}

function haveTool(tool) {
  const r = spawnSync('sh', ['-c', `command -v ${tool}`], { encoding: 'utf8' });
  return !r.error && r.status === 0;
}

function pidFromText(text) {
  const m = text.match(/pid=(\d+)/) || text.match(/^p(\d+)/m);
  return m ? parseInt(m[1]) : null;
}

function commOf(pid) {
  if (!pid) return null;
  const r = spawnSync('ps', ['-p', String(pid), '-o', 'comm='], { encoding: 'utf8' });
  if (r.error || r.status !== 0) return null;
  return (r.stdout || '').trim() || null;
}

// { state: 'free' | 'busy' | 'unknown', pid, cmd, reason }
function probePort(port) {
  let sawWorkingTool = false;
  let pid = null, cmd = null, busy = false;

  if (haveTool('ss')) {
    const r = spawnSync('sh', ['-c', `ss -tlnp "sport = :${port}"`], { encoding: 'utf8' });
    if (!r.error && r.status === 0) {
      sawWorkingTool = true;
      const rows = (r.stdout || '').trim().split('\n').slice(1).filter(l => l.trim());
      if (rows.length) { busy = true; pid = pid ?? pidFromText(rows.join('\n')); }
    }
  }

  if (haveTool('lsof')) {
    const r = spawnSync('sh', ['-c', `lsof -i :${port} -sTCP:LISTEN -Fpc`], { encoding: 'utf8' });
    // lsof exits 1 for "found nothing", which is a real answer, not a failure.
    if (!r.error && (r.status === 0 || r.status === 1)) {
      sawWorkingTool = true;
      const out = (r.stdout || '').trim();
      if (out) {
        busy = true;
        pid = pid ?? pidFromText(out);
        const cm = out.match(/^c(.+)$/m);
        if (cm) cmd = cm[1];
      }
    }
  }

  if (!sawWorkingTool) {
    return { state: 'unknown', pid: null, cmd: null,
             reason: 'neither ss nor lsof could be run — cannot tell if this port is in use' };
  }
  if (!busy) return { state: 'free', pid: null, cmd: null };
  return { state: 'busy', pid, cmd: cmd || commOf(pid) };
}

// Kept for the call sites that only care about the port being usable. An
// UNKNOWN state is deliberately NOT free — callers must handle it.
function isPortFree(port) {
  return probePort(port).state === 'free';
}

function getAllListeningPorts() {
  try {
    const ssOut = execSync(`ss -tlnp 2>/dev/null`, { encoding: 'utf8', stdio: ['pipe','pipe','pipe'] }).trim();
    const ssPorts = new Map();
    for (const line of ssOut.split('\n').slice(1)) {
      const portMatch = line.match(/[:\[](\d+)\s+\S+:\*/);
      const pNum = portMatch ? parseInt(portMatch[1]) : null;
      if (!pNum) continue;
      const pidMatch = line.match(/pid=(\d+)/);
      if (!ssPorts.has(pNum)) ssPorts.set(pNum, pidMatch ? parseInt(pidMatch[1]) : null);
    }

    const lsofOut = execSync(`lsof -iTCP -sTCP:LISTEN -Pn -Fpcn 2>/dev/null`, { encoding: 'utf8', stdio: ['pipe','pipe','pipe'] }).trim();
    const portToCmd = {};
    const portToPid = {};
    let curPid = null, curCmd = null;
    for (const line of lsofOut.split('\n')) {
      if (line.startsWith('p')) { curPid = parseInt(line.slice(1)); }
      else if (line.startsWith('c')) { curCmd = line.slice(1); }
      else if (line.startsWith('n')) {
        const m = line.match(/:(\d+)$/);
        if (m) {
          const p = parseInt(m[1]);
          if (!portToCmd[p]) { portToCmd[p] = curCmd; portToPid[p] = curPid; }
        }
      }
    }

    const ports = [...ssPorts.keys()].sort((a, b) => a - b);
    for (const p of ports) {
      if (!portToPid[p] && ssPorts.get(p)) portToPid[p] = ssPorts.get(p);
      if (!portToCmd[p] && portToPid[p]) portToCmd[p] = commOf(portToPid[p]);
    }

    return { ports, portToPid, portToCmd };
  } catch {
    return { ports: [], portToPid: {}, portToCmd: {} };
  }
}

function findDockerContainerOnPort(port) {
  try {
    const out = execSync(`docker ps --format '{{.ID}}\t{{.Names}}\t{{.Ports}}' 2>/dev/null`, { encoding: 'utf8', stdio: ['pipe','pipe','pipe'] }).trim();
    if (!out) return null;
    for (const line of out.split('\n')) {
      if (line.includes(`:${port}->`)) {
        const [id, name] = line.split('\t');
        return { id: id.trim(), name: name.trim() };
      }
    }
    return null;
  } catch { return null; }
}

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

// { ok, reason }. Never escalates to SIGKILL when SIGTERM already worked, and
// refuses system ports unless forced.
function killPort(port, { force = false } = {}) {
  const container = findDockerContainerOnPort(port);
  if (container) {
    console.log(clr('gray', `  → Docker container: ${container.name} (${container.id})`));
    try {
      execSync(`docker stop ${container.id}`, { stdio: 'pipe' });
      return { ok: true, reason: `stopped container ${container.name}` };
    } catch (e) {
      return { ok: false, reason: `docker stop failed: ${e.message}` };
    }
  }

  const probe = probePort(port);
  if (probe.state === 'unknown') return { ok: false, reason: probe.reason };
  if (probe.state === 'free')    return { ok: true,  reason: 'already free' };

  if (PROTECTED_PORTS.has(port) && !force) {
    return { ok: false, reason:
      `port ${port} is a system/database port (holder: ${probe.cmd || 'unknown'}). ` +
      `Refusing. Re-run with --force if you really mean it.` };
  }
  if (!probe.pid) {
    return { ok: false, reason: 'held by a process this user cannot see — try: sudo fuser -k ' + port + '/tcp' };
  }

  try {
    process.kill(probe.pid, 'SIGTERM');
  } catch (e) {
    if (e.code === 'EPERM') return { ok: false, reason: `not permitted to signal pid ${probe.pid} (owned by another user)` };
    if (e.code === 'ESRCH') return { ok: true,  reason: 'process had already exited' };
    return { ok: false, reason: e.message };
  }

  for (let i = 0; i < 25; i++) {          // up to 5s of grace
    if (!alive(probe.pid)) return { ok: true, reason: 'stopped cleanly (SIGTERM)' };
    sleepMs(200);
  }
  try { process.kill(probe.pid, 'SIGKILL'); } catch {}
  sleepMs(200);
  return alive(probe.pid)
    ? { ok: false, reason: `pid ${probe.pid} survived SIGKILL` }
    : { ok: true,  reason: 'killed (SIGKILL after 5s grace)' };
}

// ── Project / group lookup ────────────────────────────────────────────────────
function findProject(reg, nameOrPort) {
  if (reg.projects[nameOrPort]) return [nameOrPort, reg.projects[nameOrPort]];
  const asPort = parseInt(nameOrPort);
  if (!isNaN(asPort)) {
    for (const [name, proj] of Object.entries(reg.projects)) {
      if (proj.port === asPort) return [name, proj];
    }
  }
  return [null, null];
}

// Returns all projects belonging to a group, or the single project if it's a plain name
function resolveGroup(reg, nameOrGroup) {
  // Exact match → single project
  if (reg.projects[nameOrGroup]) return [[nameOrGroup, reg.projects[nameOrGroup]]];
  // Group match → all members
  const members = Object.entries(reg.projects).filter(([, p]) => p.group === nameOrGroup);
  return members;
}

function getGroups(reg) {
  const groups = {};
  for (const [name, proj] of Object.entries(reg.projects)) {
    const g = proj.group || null;
    if (g) {
      if (!groups[g]) groups[g] = [];
      groups[g].push([name, proj]);
    }
  }
  return groups;
}

// ── Status badge ──────────────────────────────────────────────────────────────
function statusBadge(port) {
  const probe = probePort(port);
  if (probe.state === 'unknown') return clr('yellow', '● unknown');
  if (probe.state === 'free')    return clr('green',  '● free');
  if (!probe.pid) {
    const container = findDockerContainerOnPort(port);
    if (container) return `${clr('red', '● in use')} ${clr('gray', `docker: ${container.name}`)}`;
    return `${clr('red', '● in use')} ${clr('gray', '(privileged process)')}`;
  }
  return `${clr('red', '● in use')} ${clr('gray', `pid=${probe.pid}${probe.cmd ? ' (' + probe.cmd + ')' : ''}`)}`;
}

// ── Commands ──────────────────────────────────────────────────────────────────

function parseAddArgs(tokens) {
  // returns { port, dir, startCmd }
  const portStr = tokens.shift();
  const port = parseInt(portStr);
  if (isNaN(port) || port < 1 || port > 65535) {
    console.error(clr('red', `Invalid port: ${portStr}`)); process.exit(1);
  }
  let dir = null, startCmd = null;
  if (tokens.length > 0 && (tokens[0].startsWith('/') || tokens[0].startsWith('.') || tokens[0].startsWith('~') || tokens[0] === '--')) {
    if (tokens[0] !== '--') dir = tokens.shift();
    if (tokens[0] === '--') tokens.shift();
    if (tokens.length > 0) startCmd = tokens.join(' ');
  } else if (tokens.length > 0) {
    startCmd = tokens.join(' ');
  }
  const resolvedDir = dir ? path.resolve(dir.replace(/^~/, os.homedir())) : process.cwd();
  return { port, dir: resolvedDir, startCmd };
}

// Refuse to double-book a port. This used to print a warning and write anyway,
// so two projects could sit on one port with nothing to stop it.
function assertPortFreeInRegistry(reg, port, key, force) {
  const clash = Object.entries(reg.projects).find(([n, p]) => p.port === port && n !== key);
  if (!clash) return;
  if (force) {
    console.warn(clr('yellow', `⚠ Port ${port} is also assigned to "${clash[0]}" — proceeding because of --force.`));
    return;
  }
  console.error(clr('red', `✘ Port ${port} is already assigned to "${clash[0]}".`));
  console.error(clr('gray', '  Pick another port, or re-run with --force to double-book it deliberately.'));
  process.exit(1);
}

function cmdAdd(argsIn) {
  const force = argsIn.includes('--force');
  const args = argsIn.filter(a => a !== '--force');
  if (args.length < 2) {
    console.error(clr('red', 'Usage:'));
    console.error(clr('gray', '  portman add <name> <port> [dir] [cmd...]          # single service'));
    console.error(clr('gray', '  portman add <group> <service> <port> [dir] [cmd]  # multi-service project'));
    process.exit(1);
  }

  const reg = loadRegistry();

  // Detect if second arg is a port number or a service name
  const secondAsPort = parseInt(args[1]);
  const isGroupForm = isNaN(secondAsPort) && args.length >= 3;

  let key, group, entry;

  if (isGroupForm) {
    // portman add <group> <service> <port> [dir] [cmd]
    const [groupName, service, ...rest] = args;
    key = `${groupName}:${service}`;
    group = groupName;
    const { port, dir, startCmd } = parseAddArgs(rest);
    assertPortFreeInRegistry(reg, port, key, force);
    const existing = reg.projects[key] || {};
    entry = { port, dir, group, startCmd: startCmd ?? existing.startCmd ?? null, addedAt: new Date().toISOString() };
  } else {
    // portman add <name> <port> [dir] [cmd]
    const [name, ...rest] = args;
    key = name;
    group = null;
    const { port, dir, startCmd } = parseAddArgs(rest);
    assertPortFreeInRegistry(reg, port, key, force);
    const existing = reg.projects[key] || {};
    entry = { port, dir, startCmd: startCmd ?? existing.startCmd ?? null, addedAt: new Date().toISOString() };
  }

  if (!fs.existsSync(entry.dir)) {
    console.warn(clr('yellow', `⚠ ${entry.dir} does not exist — "${key}" will not start until it does.`));
  }

  reg.projects[key] = entry;
  saveRegistry(reg);

  const groupLabel = group ? clr('gray', ` [group: ${group}]`) : '';
  console.log(`${clr('green', '✔')} Registered ${bold(key)} → port ${bold(entry.port)}${groupLabel} (${clr('gray', entry.dir)})`);
  if (entry.startCmd) {
    console.log(`   cmd: ${clr('gray', entry.startCmd)}`);
  } else {
    console.log(clr('gray', `   tip: portman set ${key} npm run dev`));
  }
}

function cmdSet(args) {
  const [name, ...cmdArgs] = args;
  if (!name || cmdArgs.length === 0) {
    console.error(clr('red', 'Usage: portman set <name> <command...>'));
    process.exit(1);
  }
  const reg = loadRegistry();
  if (!reg.projects[name]) { console.error(clr('red', `Project "${name}" not found`)); process.exit(1); }
  reg.projects[name].startCmd = cmdArgs.join(' ');
  saveRegistry(reg);
  console.log(`${clr('green', '✔')} Start command for ${bold(name)} set to: ${clr('gray', cmdArgs.join(' '))}`);
}

function cmdRemove(args) {
  const [name] = args;
  if (!name) { console.error(clr('red', 'Usage: portman remove <name|group>')); process.exit(1); }
  const reg = loadRegistry();
  const members = resolveGroup(reg, name);
  if (members.length === 0) { console.error(clr('red', `Project or group "${name}" not found`)); process.exit(1); }

  for (const [sname, proj] of members) {
    if (probePort(proj.port).state === 'busy') {
      console.log(`Stopping ${bold(sname)} on port ${bold(proj.port)}...`);
      const res = killPort(proj.port);
      if (!res.ok) console.error(clr('yellow', `  ⚠ could not stop it: ${res.reason}`));
    }
    delete reg.projects[sname];
    console.log(`${clr('green', '✔')} Removed ${bold(sname)}`);
  }
  saveRegistry(reg);
}

function cmdRename(args) {
  const [oldName, newName] = args;
  if (!oldName || !newName) { console.error(clr('red', 'Usage: portman rename <old> <new>')); process.exit(1); }
  const reg = loadRegistry();
  if (!reg.projects[oldName]) { console.error(clr('red', `Project "${oldName}" not found`)); process.exit(1); }
  if (reg.projects[newName]) { console.error(clr('red', `Project "${newName}" already exists`)); process.exit(1); }
  reg.projects[newName] = reg.projects[oldName];
  delete reg.projects[oldName];
  saveRegistry(reg);
  console.log(`${clr('green', '✔')} Renamed ${bold(oldName)} → ${bold(newName)}`);
}

function cmdList(args = []) {
  const reg = loadRegistry();
  const projects = Object.entries(reg.projects);

  if (args.includes('--json')) {
    console.log(JSON.stringify({
      registry: REGISTRY_FILE,
      schemaVersion: reg.schemaVersion ?? null,
      projects: projects.map(([name, p]) => {
        const probe = probePort(p.port);
        return { name, port: p.port, dir: p.dir, group: p.group ?? null,
                 startCmd: p.startCmd ?? null, scope: p.scope ?? 'fleet',
                 dirExists: !!p.dir && fs.existsSync(p.dir),
                 state: probe.state, pid: probe.pid, holder: probe.cmd };
      }),
    }, null, 2));
    return;
  }
  if (projects.length === 0) {
    console.log(clr('gray', 'No projects registered. Use: portman add <name> <port> [dir]'));
    return;
  }

  const maxName = Math.max(...projects.map(([n]) => n.length), 7);
  const maxPort = 6;
  const header = () => {
    console.log(
      bold(clr('white', 'SERVICE'.padEnd(maxName + 2))) +
      bold(clr('white', 'PORT'.padEnd(maxPort + 2))) +
      bold(clr('white', 'STATUS'.padEnd(20))) +
      bold(clr('white', 'DIRECTORY'))
    );
    console.log(clr('gray', '─'.repeat(80)));
  };

  const printRow = (name, proj) => {
    const badge = statusBadge(proj.port);
    const dir = proj.dir.replace(os.homedir(), '~');
    const cmdHint = proj.startCmd ? clr('gray', `  [${proj.startCmd}]`) : '';
    console.log(
      clr('cyan', name.padEnd(maxName + 2)) +
      clr('yellow', String(proj.port).padEnd(maxPort + 2)) +
      badge.padEnd(badge.length + Math.max(0, 20 - stripAnsi(badge).length)) +
      '  ' + clr('gray', dir) + cmdHint
    );
  };

  console.log();

  // Separate grouped vs standalone
  const groups = getGroups(reg);
  const standalone = projects.filter(([, p]) => !p.group);
  const printedGroups = new Set();

  header();

  // Print grouped projects together
  for (const [, proj] of projects) {
    if (!proj.group) continue;
    const g = proj.group;
    if (printedGroups.has(g)) continue;
    printedGroups.add(g);

    // Group header
    const members = groups[g];
    const states = members.map(([, p]) => probePort(p.port).state);
    const allRunning = states.every(s => s === 'busy');
    const anyRunning = states.some(s => s === 'busy');
    const dot = allRunning ? clr('green', '●') : anyRunning ? clr('yellow', '◑') : clr('gray', '○');
    console.log(`${dot} ${bold(clr('white', g))} ${clr('gray', `(${members.length} services)`)}`);
    for (const [sname, sproj] of members) {
      const label = sname.replace(`${g}:`, '');
      printRow('  ' + label, sproj);
    }
  }

  // Print standalone projects
  if (standalone.length > 0) {
    if (printedGroups.size > 0) console.log(clr('gray', '─'.repeat(80)));
    for (const [name, proj] of standalone) {
      printRow(name, proj);
    }
  }

  console.log();
}

function cmdStatus() {
  console.log();
  console.log(bold('All listening ports on this machine:'));
  console.log(clr('gray', '─'.repeat(60)));

  const reg = loadRegistry();
  const { ports: activePorts, portToPid, portToCmd } = getAllListeningPorts();

  if (activePorts.length === 0) {
    console.log(clr('gray', '  No listening ports found'));
    console.log();
    return;
  }

  const portToProject = {};
  for (const [name, proj] of Object.entries(reg.projects)) {
    portToProject[proj.port] = name;
  }

  for (const port of activePorts) {
    const projName = portToProject[port];
    const label = projName ? clr('cyan', ` (${projName})`) : '';
    const pid = portToPid[port];
    const cmd = portToCmd[port];
    const pidStr = pid ? clr('gray', ` pid=${pid}`) : '';
    const cmdStr = cmd ? clr('gray', ` ${cmd}`) : '';
    console.log(`  ${clr('yellow', String(port).padEnd(7))}${label}${pidStr}${cmdStr}`);
  }
  console.log();
}

function cmdKill(args) {
  const force = args.includes('--force');
  const [target] = args.filter(a => a !== '--force');
  if (!target) { console.error(clr('red', 'Usage: portman kill <port|name> [--force]')); process.exit(1); }

  const reg = loadRegistry();
  let port;

  const asPort = parseInt(target);
  if (!isNaN(asPort)) {
    port = asPort;
  } else {
    const [, proj] = findProject(reg, target);
    if (!proj) { console.error(clr('red', `Unknown project: ${target}`)); process.exit(1); }
    port = proj.port;
  }

  const probe = probePort(port);
  if (probe.state === 'unknown') {
    console.error(clr('red', `✘ Cannot determine the state of port ${port}: ${probe.reason}`));
    process.exit(2);
  }
  if (probe.state === 'free') {
    console.log(clr('green', `Port ${port} is already free.`));
    return;
  }

  console.log(`Killing process on port ${bold(port)} ${clr('gray', `(${probe.cmd || 'unknown'}, pid=${probe.pid ?? '?'})`)}...`);
  const res = killPort(port, { force });
  if (res.ok) {
    console.log(clr('green', `✔ ${res.reason}. Port ${port} is now free.`));
  } else {
    console.error(clr('red', `✘ ${res.reason}`));
    process.exit(1);
  }
}

function cmdFree(args) {
  const force = args.includes('--force');
  const [name] = args.filter(a => a !== '--force');
  if (!name) { console.error(clr('red', 'Usage: portman free <name> [--force]')); process.exit(1); }

  const reg = loadRegistry();
  const [, proj] = findProject(reg, name);
  if (!proj) { console.error(clr('red', `Unknown project: ${name}`)); process.exit(1); }

  const { port } = proj;
  const probe = probePort(port);
  if (probe.state === 'unknown') {
    console.error(clr('red', `✘ Cannot determine the state of port ${port}: ${probe.reason}`));
    process.exit(2);
  }
  if (probe.state === 'free') {
    console.log(clr('green', `✔ Port ${port} is already free for "${name}".`));
    return;
  }

  console.log(`Freeing port ${bold(port)} for ${bold(name)}...`);
  const res = killPort(port, { force });
  if (res.ok) console.log(clr('green', `✔ ${res.reason}. Port ${port} is now free.`));
  else { console.error(clr('red', `✘ Could not free port ${port}: ${res.reason}`)); process.exit(1); }
}

// Frameworks that need an explicit port flag rather than just the PORT env var.
// Operates on the command STRING: splitting on spaces used to destroy quoted
// arguments, and several fleet directories contain spaces.
function injectPort(cmd, port) {
  // Already carries an explicit port — don't second-guess it.
  if (/--port[ =]|\s-p\s+\d|runserver\s+\S*:\d+/.test(cmd)) return cmd;
  if (/\bnext\s+dev\b/.test(cmd))          return `${cmd} --port ${port}`;
  if (/manage\.py\s+runserver/.test(cmd))  return `${cmd} 0.0.0.0:${port}`;
  if (/\bflask\s+run\b/.test(cmd))         return `${cmd} --port ${port}`;
  if (/\bvite\b/.test(cmd))                return `${cmd} --port ${port}`;
  if (/\bartisan\s+serve\b/.test(cmd))     return `${cmd} --port=${port}`;
  // Everything else (Express, turbo, uv, custom) reads the PORT env var.
  return cmd;
}

// Resolve the command string for a project, or exit with advice.
function resolveCmd(name, proj, override) {
  const cmd = (override && override.length) ? override.join(' ') : proj.startCmd;
  if (!cmd) {
    console.error(clr('red', `No start command for "${name}". Set one with:`));
    console.error(clr('gray', `  portman set ${name} npm run dev`));
    process.exit(1);
  }
  return cmd;
}

// Make sure the port is ours to use. Returns true if we may proceed.
function claimPort(name, port, { force = false } = {}) {
  const probe = probePort(port);
  if (probe.state === 'free') return true;
  if (probe.state === 'unknown') {
    console.error(clr('red', `✘ Refusing to start ${name}: ${probe.reason}`));
    console.error(clr('gray', '  Install iproute2 (ss) or lsof, or pass --force to start anyway.'));
    return force;
  }
  const label = probe.pid ? `pid=${probe.pid} (${probe.cmd || '?'})` : 'privileged process';
  console.log(`${clr('yellow', '⚠')} Port ${bold(port)} in use (${label}). Freeing...`);
  const res = killPort(port, { force });
  if (!res.ok) {
    console.error(clr('red', `✘ Could not free port ${port}: ${res.reason}`));
    return false;
  }
  console.log(clr('green', `✔ Port ${port} freed (${res.reason}).`));
  return true;
}

// Start one service in the FOREGROUND (blocking). Used for a single service.
function doStart(name, cmdArgs, { force = false } = {}) {
  const reg = loadRegistry();
  const [, proj] = findProject(reg, name);
  if (!proj) { console.error(clr('red', `Unknown project: ${name}`)); process.exit(1); }

  const { port, dir } = proj;

  // A missing directory used to print the whole "Starting..." banner, run
  // nothing, and exit 0 — so any script wrapping portman saw success.
  if (!fs.existsSync(dir)) {
    console.error(clr('red', `✘ Cannot start ${bold(name)}: directory does not exist`));
    console.error(clr('gray', `    ${dir}`));
    console.error(clr('gray', `  The registry is stale. Fix it with:  portman doctor`));
    process.exit(1);
  }

  const cmd = resolveCmd(name, proj, cmdArgs);

  // Only persist an inline command if it looks like a real command.
  if (cmdArgs.length > 0 && isNaN(Number(cmdArgs[0])) && !cmdArgs[0].startsWith('-') && cmd !== proj.startCmd) {
    reg.projects[name].startCmd = cmd;
    saveRegistry(reg);
  }

  if (!claimPort(name, port, { force })) process.exit(1);

  const resolvedCmd = injectPort(cmd, port);

  console.log(`${clr('green', '▶')} Starting ${bold(name)} on port ${bold(port)}`);
  console.log(clr('gray', `  dir: ${dir}`));
  console.log(clr('gray', `  cmd: ${resolvedCmd}`));
  console.log(clr('gray', '─'.repeat(60)));

  // A single command STRING with shell:true — node runs `sh -c "<string>"`,
  // so quoting survives. The old argv-array form was re-split by the shell.
  const result = spawnSync(resolvedCmd, {
    cwd: dir,
    stdio: 'inherit',
    env: { ...process.env, PORT: String(port) },
    shell: true,
  });

  if (result.error) {
    console.error(clr('red', `✘ Failed to launch ${name}: ${result.error.message}`));
    process.exit(1);
  }
  if (result.status === null && result.signal) {
    console.error(clr('yellow', `${name} terminated by signal ${result.signal}`));
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

// Start one service DETACHED, so a group can start every member. The old group
// path called the blocking doStart() in a loop and then process.exit()ed, so
// only the first service in a group ever started.
function doStartDetached(name, { force = false } = {}) {
  const reg = loadRegistry();
  const [, proj] = findProject(reg, name);
  if (!proj) { console.error(clr('red', `  ✘ ${name}: not registered`)); return false; }

  const { port, dir } = proj;
  if (!fs.existsSync(dir)) {
    console.error(clr('red', `  ✘ ${name}: directory does not exist — ${dir}`));
    console.error(clr('gray', `     run: portman doctor`));
    return false;
  }
  if (!proj.startCmd) {
    console.error(clr('red', `  ✘ ${name}: no start command (portman set ${name} npm run dev)`));
    return false;
  }
  if (!claimPort(name, port, { force })) return false;

  const resolvedCmd = injectPort(proj.startCmd, port);
  const logDir = path.join(os.homedir(), '.portman-logs');
  fs.mkdirSync(logDir, { recursive: true });
  const logFile = path.join(logDir, `${name.replace(/[^\w.-]/g, '_')}.log`);
  const out = fs.openSync(logFile, 'a');

  const child = spawn(resolvedCmd, {
    cwd: dir,
    stdio: ['ignore', out, out],
    env: { ...process.env, PORT: String(port) },
    shell: true,
    detached: true,
  });
  child.unref();

  // Give it a moment to fail fast (bad command, instant crash).
  sleepMs(700);
  if (!alive(child.pid)) {
    console.error(clr('red', `  ✘ ${name}: exited immediately — see ${logFile}`));
    return false;
  }
  console.log(`  ${clr('green', '▶')} ${bold(name)} on port ${bold(port)} ${clr('gray', `pid=${child.pid}  log: ${logFile}`)}`);
  return true;
}

function cmdStart(args) {
  const force = args.includes('--force');
  const rest = args.filter(a => a !== '--force');
  const [name, ...cmdArgs] = rest;
  if (!name) { console.error(clr('red', 'Usage: portman start <name|group> [command...] [--force]')); process.exit(1); }
  const reg = loadRegistry();
  const members = resolveGroup(reg, name);
  if (members.length === 0) { console.error(clr('red', `Unknown project or group: ${name}`)); process.exit(1); }

  if (members.length > 1) {
    console.log(`${clr('green', '▶')} Starting group ${bold(name)} (${members.length} services, detached)...`);
    let failed = 0;
    for (const [sname] of members) {
      if (!doStartDetached(sname, { force })) failed++;
    }
    console.log();
    if (failed) {
      console.error(clr('red', `✘ ${failed} of ${members.length} services failed to start.`));
      process.exit(1);
    }
    console.log(clr('green', `✔ All ${members.length} services started. Stop them with: portman stop ${name}`));
    return;
  }
  doStart(members[0][0], cmdArgs, { force });
}

function cmdStop(args) {
  const force = args.includes('--force');
  const [name] = args.filter(a => a !== '--force');
  if (!name) { console.error(clr('red', 'Usage: portman stop <name|group> [--force]')); process.exit(1); }
  const reg = loadRegistry();
  const members = resolveGroup(reg, name);
  if (members.length === 0) { console.error(clr('red', `Unknown project or group: ${name}`)); process.exit(1); }

  let failed = 0;
  for (const [sname, proj] of members) {
    const probe = probePort(proj.port);
    if (probe.state === 'free') { console.log(clr('gray', `  ${sname}: already stopped`)); continue; }
    if (probe.state === 'unknown') {
      console.error(clr('red', `  ✘ ${sname}: ${probe.reason}`)); failed++; continue;
    }
    const res = killPort(proj.port, { force });
    if (res.ok) console.log(clr('green', `✔ Stopped ${bold(sname)} — ${res.reason} (port ${proj.port} free).`));
    else { console.error(clr('red', `✘ Failed to stop ${sname}: ${res.reason}`)); failed++; }
  }
  if (failed) process.exit(1);
}

function cmdRestart(args) {
  const force = args.includes('--force');
  const rest = args.filter(a => a !== '--force');
  const [name, ...cmdArgs] = rest;
  if (!name) { console.error(clr('red', 'Usage: portman restart <name|group>')); process.exit(1); }
  const reg = loadRegistry();
  const members = resolveGroup(reg, name);
  if (members.length === 0) { console.error(clr('red', `Unknown project or group: ${name}`)); process.exit(1); }
  console.log(`${clr('yellow', '↺')} Restarting ${bold(name)}...`);
  cmdStop([name, ...(force ? ['--force'] : [])]);
  cmdStart(rest.concat(force ? ['--force'] : []));
}

function cmdCheck(args) {
  const [portStr] = args;
  if (!portStr) { console.error(clr('red', 'Usage: portman check <port>')); process.exit(1); }
  const port = parseInt(portStr);
  if (isNaN(port)) { console.error(clr('red', 'Invalid port')); process.exit(1); }

  const reg = loadRegistry();
  const proj = Object.entries(reg.projects).find(([, p]) => p.port === port);
  if (proj) console.log(`Registered: ${bold(proj[0])} → ${clr('gray', proj[1].dir)}`);

  const probe = probePort(port);
  if (probe.state === 'unknown') {
    console.error(clr('yellow', `? Port ${port}: ${probe.reason}`));
    process.exit(2);
  }
  if (probe.state === 'free') {
    console.log(clr('green', `✔ Port ${port} is free.`));
    return;
  }
  let detail;
  if (probe.pid) {
    detail = ` pid=${probe.pid}${probe.cmd ? ' (' + probe.cmd + ')' : ''}`;
  } else {
    const container = findDockerContainerOnPort(port);
    detail = container ? ` docker container: ${container.name} (${container.id})` : ' (privileged process — needs sudo to inspect)';
  }
  console.log(clr('red', `✘ Port ${port} is in use`) + clr('gray', detail));
}

function cmdPort(args) {
  const [name] = args;
  if (!name) { console.error(clr('red', 'Usage: portman port <name>')); process.exit(1); }
  const reg = loadRegistry();
  const [, proj] = findProject(reg, name);
  if (!proj) { console.error(clr('red', `Unknown project: ${name}`)); process.exit(1); }
  console.log(proj.port);
}

function cmdTray(args) {
  const sub = args[0] || 'start';
  const result = spawnSync('portman-tray', [sub], { stdio: 'inherit' });
  process.exit(result.status ?? 0);
}

function cmdUninstall() {
  const uninstallScript = path.join(__dirname, 'uninstall.sh');
  if (!fs.existsSync(uninstallScript)) {
    console.error(clr('red', `Uninstall script not found at ${uninstallScript}`));
    console.error(clr('gray', 'Run manually: bash ~/.local/share/portman/uninstall.sh'));
    process.exit(1);
  }
  const result = spawnSync('bash', [uninstallScript], { stdio: 'inherit' });
  process.exit(result.status ?? 0);
}

// ── FLEET.json — the source of truth for dev ports ────────────────────────────
function loadFleet() {
  const spine = findSpine();
  if (!spine) return { error: 'FLEET.json not found. Set PORTMAN_SPINE=/path/to/spine.' };
  const file = path.join(spine, 'FLEET.json');
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return { error: `${file} is not valid JSON (${e.message})` };
  }
  if (!Array.isArray(data.products)) return { error: `${file} has no products[]` };
  return { spine, file, products: data.products };
}

// Expand FLEET products into the registry entries they imply.
// [{ key, name, port, dir, group, localMissing }]
function fleetEntries(fleet) {
  const out = [];
  for (const p of fleet.products) {
    if (!p.local) continue;                       // nothing to run locally
    if (p.dev_port === null || p.dev_port === undefined) continue;  // deliberately portless
    const dir = path.join(fleet.spine, p.local);
    const localMissing = !fs.existsSync(dir);
    if (p.dev_services && typeof p.dev_services === 'object') {
      // A service is either a bare port, or { port, dir } where dir is
      // relative to the product's local path (a product may have web/ and ops/).
      for (const [svc, spec] of Object.entries(p.dev_services)) {
        const port = typeof spec === 'object' ? spec.port : spec;
        const svcDir = (typeof spec === 'object' && spec.dir) ? path.join(dir, spec.dir) : dir;
        out.push({ key: `${p.name}:${svc}`, name: p.name, port, dir: svcDir,
                   group: p.name, localMissing: !fs.existsSync(svcDir), service: svc });
      }
    } else {
      out.push({ key: p.name, name: p.name, port: p.dev_port, dir, group: null, localMissing });
    }
  }
  return out;
}

// Guess a start command from what is actually in the directory.
function inferStartCmd(dir, service) {
  try {
    // Laravel first: a Laravel app has BOTH artisan and a package.json whose
    // dev script is vite (an asset builder). The port you open is artisan's.
    if (fs.existsSync(path.join(dir, 'artisan'))) return 'php artisan serve';
    const pkgPath = path.join(dir, 'package.json');
    if (fs.existsSync(pkgPath)) {
      const scripts = (JSON.parse(fs.readFileSync(pkgPath, 'utf8')).scripts) || {};
      if (scripts.dev) return 'npm run dev';
      if (scripts.start) return 'npm start';
    }
    if (fs.existsSync(path.join(dir, 'docker-compose.yml'))) return 'docker compose up';
  } catch {}
  return null;   // never guess blindly — doctor reports it as missing
}

// A start command may carry a hardcoded port flag from a previous assignment.
// Rewrite it to the current port rather than silently binding the old one.
function retargetPortFlag(cmd, port) {
  if (!cmd) return { cmd, changed: false, from: null };
  const m = cmd.match(/--port[= ](\d{2,5})|(?:^|\s)-p\s+(\d{2,5})|runserver\s+\S*?:(\d{2,5})/);
  if (!m) return { cmd, changed: false, from: null };
  const found = parseInt(m[1] || m[2] || m[3]);
  if (found === port) return { cmd, changed: false, from: found };
  const next = cmd
    .replace(/(--port[= ])(\d{2,5})/, `$1${port}`)
    .replace(/((?:^|\s)-p\s+)(\d{2,5})/, `$1${port}`)
    .replace(/(runserver\s+\S*?:)(\d{2,5})/, `$1${port}`);
  return { cmd: next, changed: true, from: found };
}

// The cwd of a listening process, so we can distinguish "this project's own
// server is running" from "an unrelated project is squatting on its port".
function cwdOfPid(pid) {
  if (!pid) return null;
  try { return fs.readlinkSync(`/proc/${pid}/cwd`); } catch { return null; }
}

// A product's own .env can disagree with its dev_port: the server then binds
// one port while the app believes it lives on another, which silently breaks
// OAuth callbacks and absolute links. Only REAL config is checked (.env,
// .env.local) — committed .example templates are documentation, not runtime.
const SELF_URL_VARS = [
  'NEXT_PUBLIC_APP_URL', 'NEXT_PUBLIC_SITE_URL', 'NEXT_PUBLIC_BASE_URL',
  'NEXT_PUBLIC_API_URL', 'APP_URL', 'AUTH_URL', 'NEXTAUTH_URL', 'SITE_URL', 'BASE_URL',
];
// Anchored at the start of the name on purpose: BASE_URL is a substring of
// DATABASE_URL, and rewriting a database port would be a real outage.
const SELF_URL_RE = new RegExp(`^\\s*(?:export\\s+)?(${SELF_URL_VARS.join('|')})\\s*=`);

function envPortMismatches(dir, port) {
  const out = [];
  for (const f of ['.env', '.env.local']) {
    const fp = path.join(dir, f);
    if (!fs.existsSync(fp)) continue;
    let lines;
    try { lines = fs.readFileSync(fp, 'utf8').split('\n'); } catch { continue; }
    lines.forEach((line, idx) => {
      if (!SELF_URL_RE.test(line)) return;
      const m = line.match(/localhost:(\d{4,5})/);
      if (!m) return;
      const found = parseInt(m[1]);
      if (found !== port) out.push({ file: f, line: idx + 1, found, text: line.trim() });
    });
  }
  return out;
}

// ── doctor ────────────────────────────────────────────────────────────────────
// Cross-checks the registry against FLEET.json and the filesystem. This is the
// check that would have caught the registry being 100% stale.
// exit 0 = clean, 1 = drift found, 2 = cannot run.
function cmdDoctor(args) {
  const asJson = args.includes('--json');
  const reg = loadRegistry();
  const fleet = loadFleet();
  const problems = [];   // { level, kind, subject, detail, fix }
  const add = (level, kind, subject, detail, fix) => problems.push({ level, kind, subject, detail, fix });

  if (fleet.error) {
    if (asJson) { console.log(JSON.stringify({ ok: false, cannotRun: fleet.error }, null, 2)); }
    else {
      console.error(clr('red', `✘ CANNOT RUN: ${fleet.error}`));
      console.error(clr('gray', '  Without FLEET.json there is nothing to audit the registry against.'));
    }
    process.exit(2);
  }

  const entries = Object.entries(reg.projects);
  const fEntries = fleetEntries(fleet);
  const fByKey = new Map(fEntries.map(e => [e.key, e]));

  if (reg.schemaVersion !== SCHEMA_VERSION) {
    add('warn', 'schema', 'registry',
        `schemaVersion is ${reg.schemaVersion ?? 'absent'}, expected ${SCHEMA_VERSION}`,
        'portman sync   (rewrites the registry with a version stamp)');
  }

  // 1. dead directories — the failure the registry was actually in
  for (const [name, proj] of entries) {
    if (proj.dir && fs.existsSync(proj.dir)) continue;
    // The right fix depends on whether FLEET.json knows a correct path for it.
    let fix, level = 'fail';
    const removable = /^\/(media|mnt|run\/media)\//.test(proj.dir || '');
    if (fByKey.has(name)) {
      fix = 'portman sync   (repoints it at the path in FLEET.json)';
    } else if (removable) {
      // An unmounted drive is not drift. Warn, so the check does not stay red
      // forever — a permanently failing alerter is one you learn to ignore.
      level = 'warn';
      fix = `probably an unmounted drive — mount it, or: portman remove ${name}`;
    } else if (proj.scope === 'personal') {
      level = 'warn';
      fix = `declared personal — portman remove ${name} if it is gone for good`;
    } else {
      fix = `not in FLEET.json, so sync cannot fix it — portman remove ${name}, or add it to FLEET.json`;
    }
    add(level, 'dead-dir', name, `directory does not exist: ${proj.dir}`, fix);
  }

  // 2. duplicate ports inside the registry
  const byPort = new Map();
  for (const [name, proj] of entries) {
    if (!byPort.has(proj.port)) byPort.set(proj.port, []);
    byPort.get(proj.port).push(name);
  }
  for (const [port, names] of byPort) {
    if (names.length > 1) {
      add('fail', 'duplicate-port', names.join(' + '), `all claim port ${port}`,
          'give each one a distinct dev_port in FLEET.json, then: portman sync');
    }
  }

  // 3. registry disagrees with FLEET.json
  for (const [name, proj] of entries) {
    const f = fByKey.get(name);
    if (!f) continue;
    if (f.port !== proj.port) {
      add('fail', 'port-drift', name,
          `registry says ${proj.port}, FLEET.json says ${f.port}`,
          'portman sync   (FLEET.json wins)');
    }
    if (path.resolve(proj.dir) !== path.resolve(f.dir)) {
      add('fail', 'dir-drift', name,
          `registry dir ${proj.dir} != FLEET.json local ${f.dir}`,
          'portman sync');
    }
  }

  // 4. FLEET products with a port that the registry does not cover
  for (const f of fEntries) {
    if (reg.projects[f.key]) continue;
    if (f.localMissing) {
      add('warn', 'no-clone', f.key,
          `FLEET.json assigns port ${f.port} but ${f.dir} does not exist locally`,
          'clone it, or clear its local path in FLEET.json');
    } else {
      add('fail', 'uncovered', f.key,
          `FLEET.json assigns port ${f.port} but it is not in the registry`,
          'portman sync');
    }
  }

  // 5. registry entries FLEET.json has never heard of
  for (const [name, proj] of entries) {
    if (fByKey.has(name)) continue;
    const base = name.split(':')[0];
    if (fleet.products.some(p => p.name === base)) {
      add('warn', 'unknown-service', name,
          `"${base}" is in FLEET.json but has no dev_services entry for this service`,
          `add it to dev_services for ${base} in FLEET.json`);
    } else if (proj.scope === 'personal') {
      // deliberately outside the fleet — declared, so not a finding
    } else {
      add('warn', 'not-in-fleet', name,
          `port ${proj.port}, not a FLEET.json product`,
          `add it to FLEET.json, or mark it: portman scope ${name} personal`);
    }
  }

  // 6a. a start command that hardcodes a different port than the registry
  for (const [name, proj] of entries) {
    const rt = retargetPortFlag(proj.startCmd, proj.port);
    if (rt.changed) {
      add('fail', 'cmd-port-mismatch', name,
          `registered on ${proj.port} but the start command forces port ${rt.from}: ${proj.startCmd}`,
          `portman set ${name} ${rt.cmd}`);
    }
  }

  // 6b. the app's own config points at a different port than it is started on
  for (const f of fEntries) {
    if (f.localMissing || f.service) continue;   // sidecars have no own .env
    for (const mm of envPortMismatches(f.dir, f.port)) {
      add('fail', 'env-port-mismatch', f.key,
          `dev_port is ${f.port} but ${mm.file}:${mm.line} says localhost:${mm.found} — ${mm.text}`,
          `set it to ${f.port}; if the app should own ${mm.found}, change dev_port in FLEET.json instead`);
    }
  }

  // 6. missing start commands
  for (const [name, proj] of entries) {
    if (!proj.startCmd) {
      add('warn', 'no-start-cmd', name, 'no start command recorded',
          `portman set ${name} npm run dev`);
    }
  }

  // 7. a registered port that some OTHER project is currently holding. Left as
  // a warning on purpose: portman must not be the thing that decides which of
  // two running dev servers wins.
  for (const [name, proj] of entries) {
    const probe = probePort(proj.port);
    if (probe.state === 'unknown') {
      add('warn', 'unprobeable', name, probe.reason, 'install iproute2 (ss) or lsof');
      continue;
    }
    if (probe.state !== 'busy' || !probe.pid) continue;
    const holderCwd = cwdOfPid(probe.pid);
    if (!holderCwd) continue;
    const own = path.resolve(proj.dir || '');
    if (own && (holderCwd === own || holderCwd.startsWith(own + path.sep))) continue;  // its own server
    add('warn', 'foreign-holder', name,
        `port ${proj.port} is held by ${probe.cmd || 'a process'} (pid ${probe.pid}) ` +
        `running in ${holderCwd}, not by ${name}`,
        `starting ${name} would kill it — give one of the two a different port`);
  }

  const fails = problems.filter(p => p.level === 'fail');
  const warns = problems.filter(p => p.level === 'warn');

  if (asJson) {
    console.log(JSON.stringify({
      ok: fails.length === 0,
      registry: REGISTRY_FILE,
      fleet: fleet.file,
      counts: { registry: entries.length, fleetPorts: fEntries.length, fail: fails.length, warn: warns.length },
      problems,
    }, null, 2));
    process.exit(fails.length ? 1 : 0);
  }

  console.log();
  console.log(bold('portman doctor'));
  console.log(clr('gray', `  registry: ${REGISTRY_FILE}  (${entries.length} entries)`));
  console.log(clr('gray', `  fleet:    ${fleet.file}  (${fEntries.length} ports assigned)`));
  console.log();

  if (!problems.length) {
    console.log(clr('green', '✔ Registry matches FLEET.json and every directory exists.'));
    console.log();
    process.exit(0);
  }

  for (const [label, list, color] of [['MUST FIX', fails, 'red'], ['WORTH A LOOK', warns, 'yellow']]) {
    if (!list.length) continue;
    console.log(bold(clr(color, `${label} (${list.length})`)));
    for (const p of list) {
      console.log(`  ${clr(color, '•')} ${bold(p.subject)} ${clr('gray', `[${p.kind}]`)}`);
      console.log(`      ${p.detail}`);
      console.log(clr('gray', `      fix: ${p.fix}`));
    }
    console.log();
  }

  if (fails.length) {
    console.log(clr('red', `✘ ${fails.length} problem(s) that will break a start.`));
    const syncFixable = fails.filter(f => f.fix.startsWith('portman sync')).length;
    if (syncFixable) console.log(clr('gray', `  ${syncFixable} of them are fixed by:  portman sync`));
    else console.log(clr('gray', '  None are fixed by sync — see the per-item fix above.'));
    console.log();
    process.exit(1);
  }
  console.log(clr('yellow', `${warns.length} warning(s), nothing broken.`));
  console.log();
  process.exit(0);
}

// ── sync ──────────────────────────────────────────────────────────────────────
// Rewrite the registry from FLEET.json. Never deletes an entry FLEET has not
// heard of — it reports it instead, so a personal project is not silently lost.
function cmdSync(args) {
  const dry = args.includes('--dry-run');
  const prune = args.includes('--prune');
  const fleet = loadFleet();
  if (fleet.error) {
    console.error(clr('red', `✘ CANNOT RUN: ${fleet.error}`));
    process.exit(2);
  }
  const reg = loadRegistry();
  const fEntries = fleetEntries(fleet);

  const created = [], updated = [], skipped = [], retargeted = [];

  for (const f of fEntries) {
    if (f.localMissing) { skipped.push(`${f.key} — no local clone at ${f.dir}`); continue; }
    const existing = reg.projects[f.key];
    let startCmd = existing?.startCmd || inferStartCmd(f.dir, f.service);
    const rt = retargetPortFlag(startCmd, f.port);
    if (rt.changed) {
      retargeted.push(`${f.key}: start command had --port ${rt.from}, rewritten to ${f.port}`);
      startCmd = rt.cmd;
    }
    const next = {
      port: f.port,
      dir: f.dir,
      ...(f.group ? { group: f.group } : {}),
      startCmd: startCmd ?? null,
      addedAt: existing?.addedAt || new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      source: 'FLEET.json',
    };
    if (!existing) {
      created.push(`${f.key} → ${f.port}  ${clr('gray', next.startCmd || '(no start command inferred)')}`);
    } else if (existing.port !== f.port || path.resolve(existing.dir || '') !== path.resolve(f.dir)) {
      updated.push(`${f.key}: port ${existing.port}→${f.port}  dir ${existing.dir} → ${f.dir}`);
    }
    if (!dry) reg.projects[f.key] = next;
  }

  const fKeys = new Set(fEntries.map(e => e.key));
  const fPorts = new Map(fEntries.map(e => [e.port, e.key]));

  // Entries FLEET.json has never heard of. Three different situations, and
  // conflating them is how a stale duplicate survives a "successful" sync.
  const stale = [], foreign = [], conflicts = [];
  for (const [name, proj] of Object.entries(reg.projects)) {
    if (fKeys.has(name)) continue;
    const dirGone = !proj.dir || !fs.existsSync(proj.dir);
    const clashesWith = fPorts.get(proj.port);
    if (clashesWith) conflicts.push({ name, proj, dirGone, clashesWith });
    else if (dirGone)  stale.push({ name, proj });
    else               foreign.push({ name, proj });
  }

  // --prune removes ONLY unambiguous stale duplicates: the directory is gone AND
  // the port already belongs to a FLEET entry. A dead directory on its own is not
  // enough — /media and /mnt paths are routinely just an unmounted drive.
  const pruned = [];
  if (prune) {
    for (const { name } of conflicts.filter(c => c.dirGone)) {
      if (!dry) delete reg.projects[name];
      pruned.push(name);
    }
  }

  console.log();
  console.log(bold(dry ? 'portman sync --dry-run' : 'portman sync'));
  console.log(clr('gray', `  source: ${fleet.file}`));
  console.log();
  const section = (title, list, color) => {
    if (!list.length) return;
    console.log(bold(clr(color, `${title} (${list.length})`)));
    for (const l of list) console.log(`  ${l}`);
    console.log();
  };
  section('created', created, 'green');
  section('repointed', updated, 'cyan');
  section('start command retargeted to the new port', retargeted, 'cyan');
  section('skipped — no local clone', skipped, 'gray');

  if (pruned.length) section('pruned — stale entries whose directory is gone', pruned, 'cyan');

  const remainingConflicts = conflicts.filter(c => !(prune && c.dirGone));
  if (remainingConflicts.length) {
    console.log(bold(clr('red', `PORT CONFLICTS (${remainingConflicts.length})`)));
    for (const c of remainingConflicts) {
      console.log(`  ${clr('red', '•')} ${bold(c.name)} holds port ${c.proj.port}, which FLEET.json assigns to ${bold(c.clashesWith)}`);
      console.log(clr('gray', `      dir: ${c.proj.dir}${c.dirGone ? '  (does not exist — almost certainly a stale duplicate)' : ''}`));
      console.log(clr('gray', `      fix: portman remove ${c.name}${c.dirGone ? '   or: portman sync --prune' : '   (or give it another port)'}`));
    }
    console.log();
  }
  if (stale.length) {
    console.log(bold(clr('yellow', `DIRECTORY MISSING, NOT IN FLEET.json (${stale.length})`)));
    console.log(clr('gray', '  Left in place — only you know whether these still matter.'));
    for (const s of stale) {
      const removable = /^\/(media|mnt|run\/media)\//.test(s.proj.dir || '');
      console.log(`  ${clr('yellow', '•')} ${bold(s.name)} → ${s.proj.port}  ${clr('gray', s.proj.dir)}`);
      if (removable) console.log(clr('gray', '      on /media or /mnt — probably just an unmounted drive, not stale'));
      console.log(clr('gray', `      if it is gone for good:  portman remove ${s.name}`));
      console.log(clr('gray', `      if it is outside the fleet:  portman scope ${s.name} personal`));
    }
    console.log();
  }
  if (foreign.length) {
    section('left alone — not in FLEET.json, directory exists',
            foreign.map(f => `${f.name} → ${f.proj.port}  ${f.proj.scope === 'personal' ? '(personal)' : `(mark it: portman scope ${f.name} personal)`}`), 'gray');
  }

  if (dry) {
    console.log(clr('gray', 'Nothing written. Re-run without --dry-run to apply.'));
    console.log();
    return;
  }
  saveRegistry(reg);
  console.log(clr('green', `✔ Registry written: ${REGISTRY_FILE}`));
  console.log(clr('gray', `  previous copy kept at ${BACKUP_FILE}`));
  console.log();
  if (remainingConflicts.length) {
    console.log(clr('red', `✘ ${remainingConflicts.length} port conflict(s) remain — resolve them or the wrong service will be killed on start.`));
    console.log();
    process.exit(1);
  }
}

// ── scope ─────────────────────────────────────────────────────────────────────
// Mark an entry as deliberately outside the fleet so doctor stops flagging it.
function cmdScope(args) {
  const [name, value] = args;
  if (!name || !value) {
    console.error(clr('red', 'Usage: portman scope <name> <personal|fleet>'));
    process.exit(1);
  }
  const reg = loadRegistry();
  if (!reg.projects[name]) { console.error(clr('red', `Project "${name}" not found`)); process.exit(1); }
  if (value === 'fleet') delete reg.projects[name].scope;
  else reg.projects[name].scope = value;
  saveRegistry(reg);
  console.log(`${clr('green', '✔')} ${bold(name)} scope: ${value}`);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function stripAnsi(str) {
  return str.replace(/\x1b\[[0-9;]*m/g, '');
}

function printHelp() {
  console.log(`
${bold('portman')} — dev server port manager

${bold('USAGE')}
  portman <command> [args]

${bold('FLEET')}
  ${clr('cyan', 'doctor')} [--json]                          Audit the registry against FLEET.json
  ${clr('cyan', 'sync')} [--dry-run] [--prune]                Rebuild the registry from FLEET.json
  ${clr('gray', '   --prune drops only stale duplicates: dead dir + a port FLEET.json owns')}
  ${clr('cyan', 'scope')} <name> <personal|fleet>            Mark an entry as outside the fleet

${bold('COMMANDS')}
  ${clr('cyan', 'add')} <name> <port> [dir] [cmd] [--force]  Register a single service
  ${clr('cyan', 'add')} <group> <service> <port> [dir] [cmd] Register a service inside a group
  ${clr('cyan', 'set')} <name> <cmd...>                      Save the start command for a service
  ${clr('cyan', 'remove')} <name|group>                      Remove service or whole group (stops first)
  ${clr('cyan', 'rename')} <old> <new>                       Rename a service
  ${clr('cyan', 'list')} [--json]                            List all projects grouped
  ${clr('cyan', 'status')}                                   Show all active listening ports
  ${clr('cyan', 'check')} <port>                             Check who is using a port
  ${clr('cyan', 'start')} <name|group> [cmd] [--force]       Start service, or all services in a group
  ${clr('cyan', 'stop')} <name|group> [--force]              Stop service or all services in group
  ${clr('cyan', 'restart')} <name|group>                     Restart service or group
  ${clr('cyan', 'kill')} <port|name> [--force]               Kill whatever is on a port
  ${clr('cyan', 'free')} <name> [--force]                    Free the registered port
  ${clr('cyan', 'port')} <name>                              Print port number (for scripting)
  ${clr('cyan', 'tray')} [start|stop|restart|status]         Manage the system tray app
  ${clr('cyan', 'uninstall')}                                Remove portman from this system

${bold('EXAMPLES')}
  # The fleet is driven from FLEET.json — assign dev_port there, then:
  portman sync --dry-run        # see what would change
  portman sync                  # apply it
  portman doctor                # dead dirs, port drift, uncovered products

  # A group starts every member, detached, logging to ~/.portman-logs/
  portman start storefront
  portman stop storefront

${bold('NOTES')}
  A group start runs detached; a single service runs in the foreground.
  System and database ports are protected from ${clr('cyan', 'kill')} unless you pass --force.
  If ss and lsof are both unavailable, portman reports ${clr('yellow', 'unknown')} and refuses
  to start rather than assuming a port is free.

${bold('REGISTRY')}
  Projects:  ${clr('gray', REGISTRY_FILE)}
  Backup:    ${clr('gray', BACKUP_FILE)}
  Source:    ${clr('gray', 'FLEET.json in the spine (PORTMAN_SPINE to override)')}
`);
}

// ── Main ──────────────────────────────────────────────────────────────────────
const [,, cmd, ...args] = process.argv;

switch (cmd) {
  case 'add':       cmdAdd(args); break;
  case 'set':       cmdSet(args); break;
  case 'remove':
  case 'rm':        cmdRemove(args); break;
  case 'rename':    cmdRename(args); break;
  case 'list':
  case 'ls':        cmdList(args); break;
  case 'status':
  case 'ps':        cmdStatus(); break;
  case 'check':     cmdCheck(args); break;
  case 'kill':      cmdKill(args); break;
  case 'free':      cmdFree(args); break;
  case 'start':     cmdStart(args); break;
  case 'stop':      cmdStop(args); break;
  case 'restart':   cmdRestart(args); break;
  case 'port':      cmdPort(args); break;
  case 'doctor':    cmdDoctor(args); break;
  case 'sync':      cmdSync(args); break;
  case 'scope':     cmdScope(args); break;
  case 'tray':      cmdTray(args); break;
  case 'uninstall': cmdUninstall(); break;
  case 'help':
  case '--help':
  case '-h':        printHelp(); break;
  default:
    if (!cmd) printHelp();
    else { console.error(clr('red', `Unknown command: ${cmd}`)); printHelp(); process.exit(1); }
}
