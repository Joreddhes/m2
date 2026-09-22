(() => {
  const kind = document.querySelector('#source-kind');
  const ftpFields = document.querySelector('.ftp-fields');
  if (kind && ftpFields) {
    const toggle = () => { ftpFields.hidden = kind.value !== 'ftp'; };
    kind.addEventListener('change', toggle); toggle();
  }

  const uploadForm = document.querySelector('#chunk-upload');
  if (uploadForm) {
    uploadForm.addEventListener('submit', async event => {
      event.preventDefault();
      const inputs = [...uploadForm.querySelectorAll('input[type="file"]')];
      const files = inputs.flatMap(input => [...input.files]);
      if (!files.length) return;
      const button = uploadForm.querySelector('button');
      const box = document.querySelector('#upload-progress');
      const label = box.querySelector('span'); const bar = box.querySelector('progress');
      const token = document.querySelector('meta[name="csrf-token"]').content;
      const destination = uploadForm.elements.destination.value;
      const chunkSize = 8 * 1024 * 1024;
      button.disabled = true; box.hidden = false;
      try {
        for (let number = 0; number < files.length; number++) {
          const file = files[number];
          const name = file.webkitRelativePath || file.name;
          label.textContent = `${number + 1}/${files.length}: ${name}`;
          let response = await fetch('/admin/api/uploads', {
            method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': token},
            body: JSON.stringify({object_id: uploadForm.dataset.objectId, destination, name, size: file.size})
          });
          let result = await response.json();
          if (!response.ok) throw new Error(result.error || 'Не удалось начать загрузку');
          let offset = result.offset;
          while (offset < file.size) {
            const chunk = file.slice(offset, Math.min(offset + chunkSize, file.size));
            response = await fetch(`/admin/api/uploads/${result.id}`, {
              method: 'PATCH', headers: {'Content-Type': 'application/octet-stream', 'Upload-Offset': String(offset), 'X-CSRFToken': token}, body: chunk
            });
            const status = await response.json();
            if (!response.ok) {
              if (response.status === 409 && Number.isFinite(status.offset)) { offset = status.offset; continue; }
              throw new Error(status.error || 'Загрузка прервана');
            }
            offset = status.offset;
            bar.value = ((number + offset / file.size) / files.length) * 100;
          }
        }
        label.textContent = 'Готово'; bar.value = 100;
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        label.textContent = `Ошибка: ${error.message}`; box.classList.add('error'); button.disabled = false;
      }
    });
  }

  const shell = document.querySelector('#player-shell');
  if (!shell) return;
  const video = document.querySelector('#video-player');
  const state = document.querySelector('#prepare-state');
  const statusText = state.querySelector('span');
  const progress = state.querySelector('progress');
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const fileId = shell.dataset.fileId;
  const playbackId = shell.dataset.playbackId;
  const audioControl = document.querySelector('#audio-control');
  const audioSelect = document.querySelector('#audio-track');
  const timeline = document.querySelector('#full-timeline');
  const timelineSeek = document.querySelector('#timeline-seek');
  const timelineCurrent = document.querySelector('#timeline-current');
  const timelineTotal = document.querySelector('#timeline-total');
  const timelineReady = document.querySelector('#timeline-ready');
  const audioPreferenceKey = 'mediahub.preferredAudioName.v1';
  let hlsPlayer = null;
  let requestGeneration = 0;
  let preferredAudio = 0;
  let preferredAudioName = null;
  let audioSelectionInitialized = false;
  let mediaRecoveryAttempted = false;
  let activeViewerId = null;
  let heartbeatTimer = null;
  let pageLeaving = false;
  let fullDuration = null;
  let preparedDuration = 0;
  let preparedTimer = null;
  let scrubbingTimeline = false;
  let blockedSeekUntil = 0;

  try { preferredAudioName = window.localStorage.getItem(audioPreferenceKey); } catch (_) {}

  const audioTrackLabel = (track, index) => {
    const label = track.name || track.label || track.lang || `Дорожка ${index + 1}`;
    return String(label).trim().toLowerCase() === 'und' ? 'default' : label;
  };
  const normalizedAudioName = name => {
    const normalized = String(name || '').trim().replace(/\s+/g, ' ').toLowerCase();
    return normalized === 'und' ? 'default' : normalized;
  };

  async function cancelViewer(viewerId) {
    if (!viewerId) return;
    try {
      await fetch('/api/playback/cancel', {
        method: 'POST', keepalive: true,
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify({viewer_id: viewerId}),
      });
    } catch (_) {}
  }

  function cancelActivePlayback() {
    const viewerId = activeViewerId;
    activeViewerId = null;
    if (heartbeatTimer !== null) {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
    return cancelViewer(viewerId);
  }

  function startHeartbeat(viewerId, taskId) {
    heartbeatTimer = window.setInterval(async () => {
      if (activeViewerId !== viewerId) return;
      try {
        const response = await fetch('/api/playback/heartbeat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
          body: JSON.stringify({viewer_id: viewerId, task_id: taskId}),
        });
        const body = await response.json();
        if (!body.active && activeViewerId === viewerId) {
          window.clearInterval(heartbeatTimer);
          heartbeatTimer = null;
          activeViewerId = null;
        }
      } catch (_) {}
    }, 20000);
  }

  function showError(message) {
    state.hidden = false;
    state.classList.add('error');
    statusText.textContent = `Ошибка: ${message}`;
  }

  function formatMediaTime(value) {
    const seconds = Math.floor(Math.max(0, value));
    const minutes = Math.floor(seconds / 60);
    const remaining = String(seconds % 60).padStart(2, '0');
    return minutes >= 60
      ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${remaining}`
      : `${minutes}:${remaining}`;
  }

  function preparedUntil() {
    if (!fullDuration) return 0;
    return Math.min(fullDuration, Math.max(0, preparedDuration));
  }

  function stopPreparedPolling() {
    if (preparedTimer !== null) {
      window.clearInterval(preparedTimer);
      preparedTimer = null;
    }
  }

  async function refreshPrepared(taskId, generation) {
    try {
      const response = await fetch(`/api/tasks/${taskId}`);
      if (!response.ok) return;
      const result = await response.json();
      if (pageLeaving || generation !== requestGeneration) return;
      const available = Number(result.prepared_until);
      if (Number.isFinite(available) && available >= 0) {
        preparedDuration = Math.max(preparedDuration, available);
      }
      if (result.status === 'done') {
        preparedDuration = fullDuration || preparedDuration;
        stopPreparedPolling();
      } else if (result.status === 'error' || result.status === 'cancelled') {
        stopPreparedPolling();
        showError(result.message || 'Обработка прервана');
      }
      updateTimeline();
    } catch (_) {}
  }

  function updateTimeline() {
    if (!timeline || !fullDuration || timeline.hidden) return;
    const available = preparedUntil();
    const current = Math.min(fullDuration, Math.max(0, video.currentTime || 0));
    const shown = scrubbingTimeline ? Number(timelineSeek.value) / 1000 * fullDuration : current;
    if (!scrubbingTimeline) timelineSeek.value = String(Math.round(current / fullDuration * 1000));
    timelineCurrent.textContent = formatMediaTime(shown);
    timelineTotal.textContent = formatMediaTime(fullDuration);
    timelineSeek.style.setProperty('--played', `${Math.min(100, shown / fullDuration * 100)}%`);
    timelineSeek.style.setProperty('--prepared', `${Math.min(100, available / fullDuration * 100)}%`);
    timelineReady.textContent = Date.now() < blockedSeekUntil
      ? `Этот участок ещё подготавливается. Доступно до ${formatMediaTime(available)}`
      : available + 0.5 >= fullDuration
        ? 'Видео полностью готово'
        : `Подготовлено до ${formatMediaTime(available)}`;
  }

  function setTimelineDuration(value) {
    if (!timeline) return;
    const duration = Number(value);
    fullDuration = Number.isFinite(duration) && duration > 0 ? duration : null;
    timeline.hidden = !fullDuration;
    updateTimeline();
  }

  function populateAudioTracks(tracks) {
    if (!tracks?.length || !hlsPlayer) {
      audioControl.hidden = true;
      return;
    }
    if (!audioSelectionInitialized) {
      const wantedName = normalizedAudioName(preferredAudioName);
      const match = wantedName
        ? tracks.findIndex((track, index) => normalizedAudioName(audioTrackLabel(track, index)) === wantedName)
        : -1;
      preferredAudio = match >= 0 ? match : 0;
      hlsPlayer.audioTrack = preferredAudio;
      audioSelectionInitialized = true;
    }
    audioSelect.replaceChildren(...tracks.map((track, index) => {
      const option = document.createElement('option');
      option.value = index;
      option.textContent = audioTrackLabel(track, index);
      option.selected = index === preferredAudio;
      return option;
    }));
    audioControl.hidden = false;
    audioSelect.disabled = tracks.length < 2;
  }

  function restorePlayback(snapshot) {
    if (!snapshot) return;
    const restore = () => {
      if (snapshot.time > 0 && Number.isFinite(snapshot.time)) video.currentTime = snapshot.time;
      if (!snapshot.paused) video.play().catch(() => {});
    };
    if (video.readyState >= 1) restore();
    else video.addEventListener('loadedmetadata', restore, {once: true});
  }

  function attach(result, generation, snapshot = null, taskId = null) {
    if (pageLeaving || generation !== requestGeneration) return;
    setTimelineDuration(result.duration);
    stopPreparedPolling();
    const initialPrepared = Number(result.prepared_until);
    preparedDuration = taskId && result.status === 'ready'
      ? (Number.isFinite(initialPrepared) && initialPrepared > 0 ? initialPrepared : 0)
      : (fullDuration || 0);
    if (taskId && result.status === 'ready') {
      preparedTimer = window.setInterval(() => refreshPrepared(taskId, generation), 4000);
    }
    const streamUrl = `/stream/${result.key}/index.m3u8`;
    if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
    audioSelectionInitialized = false;
    mediaRecoveryAttempted = false;
    video.removeAttribute('src');
    video.load();
    updateTimeline();
    video.querySelectorAll('track').forEach(track => track.remove());

    if (window.Hls && Hls.isSupported()) {
      hlsPlayer = new Hls({startPosition: snapshot?.time || 0, maxBufferLength: 30, backBufferLength: 30});
      hlsPlayer.on(Hls.Events.AUDIO_TRACKS_UPDATED, (_event, data) => populateAudioTracks(data.audioTracks));
      hlsPlayer.on(Hls.Events.MANIFEST_PARSED, () => {
        populateAudioTracks(hlsPlayer.audioTracks);
        restorePlayback(snapshot);
      });
      hlsPlayer.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !mediaRecoveryAttempted) {
          mediaRecoveryAttempted = true;
          hlsPlayer.recoverMediaError();
          return;
        }
        showError(data.error?.message || data.details || 'ошибка воспроизведения');
      });
      hlsPlayer.loadSource(streamUrl);
      hlsPlayer.attachMedia(video);
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      audioControl.hidden = true;
      video.src = streamUrl;
      restorePlayback(snapshot);
    } else {
      showError('Этот браузер не поддерживает HLS.');
      return;
    }

    (result.tracks || []).forEach(track => {
      const element = document.createElement('track');
      element.kind = 'subtitles';
      element.label = track.label;
      element.srclang = track.language;
      element.src = `/stream/${result.key}/${track.file}`;
      video.appendChild(element);
    });

    const bitmapControl = document.querySelector('#bitmap-control');
    const bitmapSelect = document.querySelector('#bitmap-track');
    if ((result.bitmap_tracks || []).length) {
      bitmapControl.hidden = false;
      const none = document.createElement('option');
      none.value = 'none';
      none.textContent = 'Выключены';
      none.selected = result.bitmap_stream === null;
      bitmapSelect.replaceChildren(none, ...result.bitmap_tracks.map(track => {
        const option = document.createElement('option');
        option.value = track.stream;
        option.textContent = `Встроить: ${track.label}`;
        option.selected = track.stream === result.bitmap_stream;
        return option;
      }));
    }
    state.hidden = true;
  }

  async function poll(taskId, generation, snapshot) {
    if (pageLeaving || generation !== requestGeneration) return;
    try {
      const response = await fetch(`/api/tasks/${taskId}`);
      const result = await response.json();
      if (pageLeaving || generation !== requestGeneration) return;
      statusText.textContent = result.message || 'Обработка…';
      progress.value = result.progress || 0;
      if (result.duration) setTimelineDuration(result.duration);
      if (result.status === 'ready' || result.status === 'done') return attach(result, generation, snapshot, taskId);
      if (result.status === 'error' || result.status === 'cancelled') return showError(result.message || 'Подготовка отменена');
      window.setTimeout(() => poll(taskId, generation, snapshot), 750);
    } catch (error) {
      if (!pageLeaving && generation === requestGeneration) showError(error.message);
    }
  }

  async function startPrepare(bitmapStream = null, snapshot = null) {
    const generation = ++requestGeneration;
    state.hidden = false;
    state.classList.remove('error');
    progress.value = 0;
    statusText.textContent = 'Подготовка первых сегментов…';
    if (timeline) timeline.hidden = true;
    stopPreparedPolling();
    preparedDuration = 0;
    video.pause();
    if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
    await cancelActivePlayback();
    if (pageLeaving || generation !== requestGeneration) return;
    const viewerId = `${playbackId}:${generation}`;
    activeViewerId = viewerId;
    try {
      const response = await fetch(`/api/media/${fileId}/prepare`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify({audio_index: preferredAudio, bitmap_stream: bitmapStream, viewer_id: viewerId}),
      });
      const body = await response.json();
      if (pageLeaving || generation !== requestGeneration) return cancelViewer(viewerId);
      if (response.status === 200) {
        activeViewerId = null;
        return attach(body, generation, snapshot);
      }
      if (response.status === 202 && body.task_id) {
        startHeartbeat(viewerId, body.task_id);
        return poll(body.task_id, generation, snapshot);
      }
      throw new Error(body.error || 'Не удалось подготовить видео');
    } catch (error) {
      if (generation === requestGeneration && !pageLeaving) {
        cancelActivePlayback();
        showError(error.message);
      }
    }
  }

  startPrepare();

  audioSelect?.addEventListener('change', event => {
    const index = Number(event.target.value);
    const track = hlsPlayer?.audioTracks?.[index];
    if (track) {
      preferredAudio = index;
      preferredAudioName = audioTrackLabel(track, index);
      try { window.localStorage.setItem(audioPreferenceKey, preferredAudioName); } catch (_) {}
      hlsPlayer.setAudioOption({
        name: track.name,
        lang: track.lang,
        groupId: track.groupId,
        channels: track.channels,
        flushImmediate: true,
      });
    }
  });
  document.querySelector('#bitmap-track')?.addEventListener('change', event => {
    const snapshot = {time: video.currentTime, paused: video.paused};
    startPrepare(event.target.value === 'none' ? null : event.target.value, snapshot);
  });

  document.querySelector('#playback-rate')?.addEventListener('change', event => {
    video.playbackRate = Number(event.target.value);
  });

  ['timeupdate', 'durationchange', 'progress', 'loadedmetadata', 'seeked'].forEach(event => {
    video.addEventListener(event, updateTimeline);
  });
  timelineSeek?.addEventListener('input', () => {
    scrubbingTimeline = true;
    updateTimeline();
  });
  timelineSeek?.addEventListener('change', () => {
    if (!fullDuration) return;
    const target = Number(timelineSeek.value) / 1000 * fullDuration;
    scrubbingTimeline = false;
    const available = preparedUntil();
    if (target > Math.max(0, available - 0.25)) {
      blockedSeekUntil = Date.now() + 3000;
    } else {
      video.currentTime = target;
      blockedSeekUntil = 0;
    }
    updateTimeline();
  });
  const timelineTimer = window.setInterval(updateTimeline, 1000);

  document.querySelectorAll('.playlist a').forEach(link => link.addEventListener('click', async event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    pageLeaving = true;
    video.pause();
    if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
    let timeoutId;
    await Promise.race([
      cancelActivePlayback(),
      new Promise(resolve => { timeoutId = window.setTimeout(resolve, 5000); }),
    ]);
    window.clearTimeout(timeoutId);
    window.location.assign(link.href);
  }));

  window.addEventListener('pagehide', () => {
    pageLeaving = true;
    window.clearInterval(timelineTimer);
    stopPreparedPolling();
    video.pause();
    if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
    cancelActivePlayback();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) window.location.reload();
  });
})();
