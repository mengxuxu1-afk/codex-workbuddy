// Fixed, project-scoped adapter for the installed WorkBuddy 5.9.3 desktop SDK.
// No account APIs, global conversation lists, permission approval, or CLI spawn.
(async () => {
  const root = __PROJECT_ROOT__, sid = __DESKTOP_SESSION__;
  const norm = x => String(x || '').replaceAll('\\', '/').replace(/\/$/, '').toLowerCase();
  const key = '__videoEditVisibleBridgeV3', version = __ADAPTER_HASH__;
  const old = globalThis[key];
  if (old) {
    if (old.root !== root || old.sid !== sid) throw Error('binding_changed');
    if (old.version === version) return true;
    if (old.active()) throw Error('active_adapter_cannot_be_replaced');
    old.dispose();
  }
  if (!globalThis.wb?.conversations?.get) throw Error('desktop_sdk_unavailable');
  const jobs = new Map();
  async function conversation() {
    const c = await wb.conversations.get(sid);
    if (!c || c.info.id !== sid || c.info.transport !== 'local' ||
        norm(c.info.space?.cwd) !== norm(root)) throw Error('conversation_binding_mismatch');
    return c;
  }
  async function metadata() {
    const c = await conversation();
    return { session_id:sid, project_root:c.info.space.cwd, state:c.info.state,
      permission_mode:await wb.config.preference('localTaskPermissionMode'),
      stored_permission_mode:c.configManager.permissionMode, pending:!!(await c.getPendingInfo()),
      background:!!c.info.hasRunningBackgroundTasks, children:!!c.info.hasActiveChildAgents,
      selected:wb.conversations.currentId === sid && wb.navigation.route === '/conversation/' + sid,
      visible:document.visibilityState === 'visible', version:globalThis.__WORKBUDDY_VERSION__ };
  }
  function projection(j) {
    return { event_id:j.event_id, request_key:j.request_key, phase:j.phase,
      started_at:j.started_at, finished_at:j.finished_at, permissions:j.permissions,
      terminal:j.terminal, prompt_visible:j.prompt_visible,
      result_keys:j.result_keys, error_code:j.error_code };
  }
  globalThis[key] = {
    root, sid, version, metadata,
    active:() => [...jobs.values()].some(j => !j.finished_at && !j.remote_terminal_seen),
    dispose:() => jobs.forEach(j => j.close?.()),
    async select() {
      await conversation();
      await wb.navigation.goto('/conversation/' + sid);
      return metadata();
    },
    async start(event, text, requestKey) {
      if (!Number.isSafeInteger(event) || event < 1 || requestKey !== 'video-edit-event-' + event ||
          typeof text !== 'string' || text.length > 16000) throw Error('invalid_assignment');
      if (jobs.has(event)) return projection(jobs.get(event));
      const m = await metadata();
      if (m.state !== 'idle' || m.pending || m.background || m.children || !m.selected ||
          m.permission_mode !== 'default-sandbox') throw Error('desktop_not_ready');
      const c = await conversation();
      const j = {event_id:event, request_key:requestKey, phase:'submitted',
        started_at:Date.now(), permissions:[], prompt_visible:false};
      jobs.set(event, j); // Persist in the renderer before the irreversible send.
      const removers = [];
      const close = () => removers.forEach(off => { if (typeof off === 'function') off(); });
      j.close=close;
      const unknown = reason => { j.unknown=true; j.phase='unknown'; j.error_code=reason; close(); };
      removers.push(c.on('pendingRequest', p => {
        if (jobs.get(event) !== j || j.finished_at || j.unknown) return;
        j.phase = p ? 'waiting_for_permission' : 'running';
        if (p && j.permissions.length < 100) j.permissions.push({
          id:String(p.id || p.requestId || ''), type:String(p.type || p.kind || 'permission'),
          observed_at:Date.now(), approved_by_bridge:false });
      }));
      const marker = '本项目协作事件 ' + event + '，';
      // Only inspect updates from this conversation AFTER registering this job.
      // Never load/replay history or confuse a pre-existing idle state with completion.
      const observe = async id => {
        if (j.finished_at || j.unknown || typeof id !== 'string') return;
        const r = await c.getRequest(id);
        if (j.finished_at || j.unknown) return;
        if (!r || !Number.isFinite(r.timestamp) || r.timestamp < j.started_at - 1000) return;
        const content = r.userMessage?.content;
        if (!Array.isArray(content) || !content.some(b =>
          b.type === 'text' && String(b.text || '').includes(marker))) return;
        if (j.native_request_id && j.native_request_id !== r.id) {
          unknown('ambiguous_native_request'); return;
        }
        j.native_request_id=r.id;
        j.phase='running';
        if (['completed','error','failed','cancelled','interrupted'].includes(r.state)) {
          j.terminal={request_id:r.id,state:r.state,request_key_matched:true};
          j.phase=r.state === 'completed' ? 'returned' : r.state;
          j.finished_at=Date.now(); close();
        }
      };
      j.observe=observe;
      removers.push(c.on('requestUpdate', id => {
        observe(typeof id === 'string' ? id : id?.requestId).catch(() => {
          unknown('native_request_observation_failed');
        });
      }));
      removers.push(c.on('stateChange', () => {
        if (j.native_request_id) observe(j.native_request_id).catch(() => {
          unknown('native_terminal_observation_failed');
        });
      }));
      // Submit through the actual desktop composer, which applies the user's
      // global default-sandbox preference and displays native permission cards.
      // This does not replace the user's draft or invoke a permission resolver.
      Promise.resolve(wb.conversations.prepareInput({
        target:{type:'conversation',conversationId:sid},
        contentBlocks:[{type:'text',text}],submitMode:'blocks'
      })).then(() => { j.composer_accepted=true; }).catch(() => {
        unknown('native_composer_rejected');
      });
      return projection(j);
    },
    async read(event) {
      const j = jobs.get(event);
      if (!j) return {event_id:event,phase:'missing_no_resubmit'};
      if (!j.finished_at && !j.unknown) {
        if (!j.native_request_id) {
          const c=await conversation(), page=await c.requests(0,1);
          if (page.items?.[0]?.id) await j.observe(page.items[0].id);
        } else await j.observe(j.native_request_id);
      }
      j.prompt_visible ||= document.body.innerText.includes('本项目协作事件 ' + event + '，');
      return projection(j);
    },
    async terminal(event, started) {
      const c=await conversation();
      const page=await c.requests(0,1), r=page.items?.[0];
      const marker='本项目协作事件 '+event+'，';
      if (!r || !Number.isFinite(r.timestamp) || r.timestamp<started-1000 ||
          !r.userMessage?.content?.some(b=>b.type==='text' && String(b.text||'').includes(marker)) ||
          !['completed','cancelled','error','failed'].includes(r.state)) return {matched:false};
      const j=jobs.get(event);
      if (j && (!j.native_request_id || j.native_request_id===r.id)) {
        j.remote_terminal_seen=true; j.close?.();
      }
      return {matched:true,event_id:event,request_id:r.id,state:r.state,
        timestamp:r.timestamp,finished_at:r.finishTimestamp,native_state:c.info.state};
    },
    async cancel(event) {
      const j = jobs.get(event);
      if (!j || j.finished_at) throw Error('no_bound_active_request');
      // The SDK only exposes conversation-wide cancel. It cannot prove that
      // the current active turn still belongs to this event. Never cancel a
      // possibly newer user turn; leave the visible native Stop control intact.
      return {event_id:event,cancel_requested:false,stopped_confirmed:false,
        reason:'native_stop_requires_current_turn_confirmation'};
    }
  };
  return true;
})()
