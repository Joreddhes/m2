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
  const audioControl = document.querySelector('#audio-control');
  const audioSelect = document.querySelector('#audio-track');
  let hlsPlayer = null;
  let requestGeneration = 0;
  let preferredAudio = 0;
  let audioSelectionInitialized = false;
  let mediaRecoveryAttempted = false;

  function showError(message) {
    state.hidden = false;
    state.classList.add('error');
    statusText.textContent = `Ошибка: ${message}`;
  }

  function populateAudioTracks(tracks) {
    if (!tracks || tracks.length < 2 || !hlsPlayer) {
      audioControl.hidden = true;
      return;
    }
    preferredAudio = Math.min(preferredAudio, tracks.length - 1);
    audioSelect.replaceChildren(...tracks.map((track, index) => {
      const option = document.createElement('option');
      option.value = index;
      option.textContent = track.name || track.label || track.lang || `Дорожка ${index + 1}`;
      option.selected = index === preferredAudio;
      return option;
    }));
    audioControl.hidden = false;
    if (!audioSelectionInitialized) {
      hlsPlayer.audioTrack = preferredAudio;
      audioSelectionInitialized = true;
    }
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

  function attach(result, generation, snapshot = null) {
    if (generation !== requestGeneration) return;
    const streamUrl = `/stream/${result.key}/index.m3u8`;
    if (hlsPlayer) { hlsPlayer.destroy(); hlsPlayer = null; }
    audioSelectionInitialized = false;
    mediaRecoveryAttempted = false;
    video.removeAttribute('src');
    video.load();
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
    if (generation !== requestGeneration) return;
    try {
      const response = await fetch(`/api/tasks/${taskId}`);
      const result = await response.json();
      if (generation !== requestGeneration) return;
      statusText.textContent = result.message || 'Обработка…';
      progress.value = result.progress || 0;
      if (result.status === 'ready' || result.status === 'done') return attach(result, generation, snapshot);
      if (result.status === 'error') return showError(result.message || 'FFmpeg завершился с ошибкой');
      window.setTimeout(() => poll(taskId, generation, snapshot), 750);
    } catch (error) {
      if (generation === requestGeneration) showError(error.message);
    }
  }

  async function startPrepare(bitmapStream = null, snapshot = null) {
    const generation = ++requestGeneration;
    state.hidden = false;
    state.classList.remove('error');
    progress.value = 0;
    statusText.textContent = 'Подготовка первых сегментов…';
    try {
      const response = await fetch(`/api/media/${fileId}/prepare`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
        body: JSON.stringify({audio_index: preferredAudio, bitmap_stream: bitmapStream}),
      });
      const body = await response.json();
      if (generation !== requestGeneration) return;
      if (response.status === 200) return attach(body, generation, snapshot);
      if (response.status === 202 && body.task_id) return poll(body.task_id, generation, snapshot);
      throw new Error(body.error || 'Не удалось подготовить видео');
    } catch (error) {
      if (generation === requestGeneration) showError(error.message);
    }
  }

  startPrepare();

  audioSelect?.addEventListener('change', event => {
    preferredAudio = Number(event.target.value);
    const track = hlsPlayer?.audioTracks?.[preferredAudio];
    if (track) {
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
})();
