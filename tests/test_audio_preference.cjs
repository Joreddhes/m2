const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const appScript = fs.readFileSync(path.join(__dirname, '../mediahub/static/app.js'), 'utf8');
const preferenceKey = 'mediahub.preferredAudioName.v1';

function storage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

async function playerPage(tracks, localStorage, result = {}, options = {}) {
  const handlers = {};
  const audioSelect = {
    children: [],
    value: '',
    replaceChildren(...children) {
      this.children = children;
      this.value = String(children.find(option => option.selected)?.value ?? '');
    },
    addEventListener(event, handler) { handlers[event] = handler; },
    choose(index) {
      this.value = String(index);
      handlers.change({target: this});
    },
  };
  const bitmapHandlers = {};
  const bitmapSelect = {
    replaceChildren() {},
    addEventListener(event, handler) { bitmapHandlers[event] = handler; },
    choose(value) { bitmapHandlers.change({target: {value}}); },
  };
  const audioControl = {hidden: true};
  const timelineHandlers = {};
  const timeline = {hidden: true};
  const timelineSeek = {
    value: '0',
    styles: {},
    style: {setProperty(name, value) { timelineSeek.styles[name] = value; }},
    addEventListener(event, handler) { timelineHandlers[event] = handler; },
    choose(value) {
      this.value = String(value);
      timelineHandlers.input();
      timelineHandlers.change();
    },
  };
  const timelineCurrent = {textContent: ''};
  const timelineTotal = {textContent: ''};
  const timelineReady = {textContent: ''};
  const state = {
    hidden: false,
    classList: {add() {}, remove() {}},
    querySelector: selector => selector === 'span' ? {textContent: ''} : {value: 0},
  };
  const video = {
    readyState: 0,
    currentTime: 0,
    duration: options.available ?? 0,
    seekable: {length: options.available ? 1 : 0, end: () => options.available},
    paused: true,
    pause() {},
    removeAttribute() {},
    load() {},
    querySelectorAll: () => [],
    appendChild() {},
    addEventListener() {},
  };
  const elements = {
    '#player-shell': {dataset: {fileId: '1', playbackId: 'test-playback'}},
    '#video-player': video,
    '#prepare-state': state,
    'meta[name="csrf-token"]': {content: 'test-token'},
    '#audio-control': audioControl,
    '#audio-track': audioSelect,
    '#full-timeline': timeline,
    '#timeline-seek': timelineSeek,
    '#timeline-current': timelineCurrent,
    '#timeline-total': timelineTotal,
    '#timeline-ready': timelineReady,
    '#bitmap-control': {hidden: true},
    '#bitmap-track': bitmapSelect,
  };
  const players = [];
  const requests = [];
  const navigation = [];
  const windowHandlers = {};
  const intervals = new Map();
  const link = {
    href: '/play/2',
    addEventListener(event, handler) { this.handler = handler; },
    click() {
      return this.handler({button: 0, defaultPrevented: false, preventDefault() {}});
    },
  };
  class FakeHls {
    static Events = {AUDIO_TRACKS_UPDATED: 'tracks', MANIFEST_PARSED: 'manifest', ERROR: 'error'};
    static isSupported() { return true; }

    constructor() {
      this.audioTracks = tracks;
      this.handlers = {};
      this.initialSelections = [];
      players.push(this);
    }

    on(event, handler) { this.handlers[event] = handler; }
    emit(event, data = {}) { this.handlers[event]?.(event, data); }
    set audioTrack(index) { this.selectedTrack = index; this.initialSelections.push(index); }
    get audioTrack() { return this.selectedTrack; }
    loadSource() {}
    attachMedia() {
      this.emit(FakeHls.Events.AUDIO_TRACKS_UPDATED, {audioTracks: this.audioTracks});
      this.emit(FakeHls.Events.MANIFEST_PARSED);
    }
    setAudioOption(option) {
      this.selectedTrack = this.audioTracks.findIndex(track => track.name === option.name);
    }
    destroy() {}
  }
  const document = {
    querySelector: selector => elements[selector] ?? null,
    querySelectorAll: () => options.withLink ? [link] : [],
    createElement: () => ({}),
  };
  const window = {
    Hls: FakeHls,
    localStorage,
    addEventListener(event, handler) { windowHandlers[event] = handler; },
    setInterval(callback) { const id = intervals.size + 1; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout,
    clearTimeout,
    location: {assign: url => navigation.push(url)},
  };
  const fetch = async (url, request) => {
    requests.push({url, request});
    if (url === '/api/playback/cancel') return {status: 200, json: async () => ({stopped: true})};
    if (url === '/api/playback/heartbeat') return {status: 200, json: async () => ({active: true})};
    if (url.startsWith('/api/tasks/')) {
      return {status: 200, ok: true, json: async () => ({status: 'ready', key: 'test-stream', tracks: [], bitmap_tracks: [], ...result})};
    }
    return options.queued
      ? {status: 202, json: async () => ({task_id: 'task-1'})}
      : {status: 200, json: async () => ({key: 'test-stream', tracks: [], bitmap_tracks: [], ...result})};
  };
  vm.runInNewContext(appScript, {document, window, Hls: FakeHls, fetch});
  await new Promise(setImmediate);
  return {audioSelect, audioControl, bitmapSelect, players, requests, navigation, link, windowHandlers,
    video, timeline, timelineSeek, timelineCurrent, timelineTotal, timelineReady,
    tick: async () => Promise.all([...intervals.values()].map(callback => callback()))};
}

test('remembers a manually selected name and finds it after track reordering', async () => {
  const saved = storage();
  const first = await playerPage([{name: 'Original'}, {name: ' Студия  А '}], saved);
  assert.equal(first.players[0].audioTrack, 0);
  first.audioSelect.choose(1);
  assert.equal(saved.getItem(preferenceKey), ' Студия  А ');

  const next = await playerPage([{name: 'Original'}, {name: 'Other'}, {name: 'студия а'}], saved);
  assert.equal(next.players[0].audioTrack, 2);
  assert.equal(next.audioSelect.value, '2');
  assert.deepEqual(next.players[0].initialSelections, [2]);
});

test('falls back to the first track without forgetting the chosen name', async () => {
  const saved = storage();
  saved.setItem(preferenceKey, 'Студия А');
  const fallback = await playerPage([{name: 'Original'}, {name: 'Other'}], saved);
  assert.equal(fallback.players[0].audioTrack, 0);
  assert.equal(saved.getItem(preferenceKey), 'Студия А');

  const singleTrack = await playerPage([{name: 'Original'}], saved);
  assert.equal(singleTrack.players[0].audioTrack, 0);
  assert.equal(singleTrack.audioControl.hidden, false);
  assert.equal(singleTrack.audioSelect.disabled, true);
  assert.equal(saved.getItem(preferenceKey), 'Студия А');

  const unnamedTrack = await playerPage([{name: 'und'}], saved);
  assert.equal(unnamedTrack.audioSelect.children[0].textContent, 'default');

  const restored = await playerPage([{name: 'Original'}, {name: 'Студия А'}], saved);
  assert.equal(restored.players[0].audioTrack, 1);
  restored.players[0].emit('manifest');
  assert.equal(restored.players[0].audioTrack, 1);
  assert.deepEqual(restored.players[0].initialSelections, [1]);
});

test('retains the selected name on reattach when browser storage is unavailable', async () => {
  const deniedStorage = {
    getItem() { throw new Error('denied'); },
    setItem() { throw new Error('denied'); },
  };
  const page = await playerPage(
    [{name: 'Original'}, {name: 'Студия А'}],
    deniedStorage,
    {bitmap_tracks: [{stream: 3, label: 'PGS'}]},
  );
  page.audioSelect.choose(1);
  page.bitmapSelect.choose('3');
  await new Promise(setImmediate);
  assert.equal(page.players.length, 2);
  assert.equal(page.players[1].audioTrack, 1);
  assert.equal(page.audioSelect.value, '1');
});

test('waits for cancellation before opening another episode', async () => {
  const page = await playerPage([{name: 'Original'}], storage(), {}, {queued: true, withLink: true});
  assert.equal(page.players.length, 1);
  await page.link.click();
  const cancel = page.requests.find(entry => entry.url === '/api/playback/cancel');
  assert.ok(cancel);
  assert.equal(JSON.parse(cancel.request.body).viewer_id, 'test-playback:1');
  assert.equal(cancel.request.keepalive, true);
  assert.deepEqual(page.navigation, ['/play/2']);
});

test('requests cancellation when the page closes', async () => {
  const page = await playerPage([{name: 'Original'}], storage(), {}, {queued: true});
  page.windowHandlers.pagehide();
  await new Promise(setImmediate);
  const cancel = page.requests.find(entry => entry.url === '/api/playback/cancel');
  assert.equal(JSON.parse(cancel.request.body).viewer_id, 'test-playback:1');
});

test('shows full duration and rejects seeks beyond prepared video', async () => {
  const result = {duration: 100, prepared_until: 20};
  const page = await playerPage([{name: 'Original'}], storage(),
    result, {available: 100, queued: true});
  assert.equal(page.timeline.hidden, false);
  assert.equal(page.timelineTotal.textContent, '1:40');
  assert.equal(page.timelineReady.textContent, 'Подготовлено до 0:20');
  assert.equal(page.timelineSeek.styles['--prepared'], '20%');

  page.timelineSeek.choose(500);
  assert.equal(page.video.currentTime, 0);
  assert.match(page.timelineReady.textContent, /ещё подготавливается/);

  page.timelineSeek.choose(100);
  assert.equal(page.video.currentTime, 10);
  assert.equal(page.timelineCurrent.textContent, '0:10');

  result.prepared_until = 50;
  await page.tick();
  assert.equal(page.timelineSeek.styles['--prepared'], '50%');
  result.status = 'done';
  await page.tick();
  assert.equal(page.timelineReady.textContent, 'Видео полностью готово');
});
