/* Small stand-in for the shared database the dashboard expects.
   Listings and refresh status come from static JSON files (written daily by the GitHub Action).
   Votes, comments, tags, prefs and custom criteria live in Supabase, or in this browser if Supabase is not configured. */
(function(){
  const CFG = window.HH_CONFIG || {};
  const REMOTE = !!(CFG.SUPABASE_URL && CFG.SUPABASE_ANON_KEY);
  const POLL_MS = 6000, STATIC_MS = 5 * 60000;
  const slug = s => String(s || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 24);
  const cap = s => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;

  /* ---- identity ---- */
  let me = null; try { me = localStorage.getItem("hh_uid"); } catch(e){}
  function ensureMe(){
    if(me) return me;
    let n = ""; try { n = window.prompt("Your first name? (this is how your votes and comments are labelled)") || ""; } catch(e){}
    me = slug(n) || ("guest-" + Math.random().toString(36).slice(2, 6));
    try { localStorage.setItem("hh_uid", me); } catch(e){}
    return me;
  }
  const userCap = {
    id: async () => ensureMe(),
    profiles: async ids => { const o = {}; (ids || []).forEach(i => o[i] = {name: cap(String(i).replace(/-/g, " "))}); return o; },
    can: async () => true,
    rename: n => { me = slug(n) || me; try { localStorage.setItem("hh_uid", me); } catch(e){} location.reload(); }
  };

  /* ---- storage ---- */
  const cache = {};            // path -> data, for dynamic collections
  const listeners = [];        // {coll, path, cb, err}
  const sharedColls = ["votes", "comments", "meta", "prefs", "shared"];
  const collOf = p => p.split("/")[0];
  const idOf = p => p.slice(p.indexOf("/") + 1);
  let staticDocs = {};         // path -> data for listings/* and config/status

  const sb = async (path, opt = {}) => {
    const r = await fetch(CFG.SUPABASE_URL + "/rest/v1/" + path, {...opt, headers: {apikey: CFG.SUPABASE_ANON_KEY, Authorization: "Bearer " + CFG.SUPABASE_ANON_KEY, "Content-Type": "application/json", ...(opt.headers || {})}});
    if(!r.ok) throw new Error("db " + r.status);
    return r.status === 204 ? null : r.json();
  };
  const LS = "hh_store_v1";
  const lsLoad = () => { try { return JSON.parse(localStorage.getItem(LS) || "{}"); } catch(e){ return {}; } };
  const lsSave = o => { try { localStorage.setItem(LS, JSON.stringify(o)); } catch(e){} };

  async function pullColl(c){
    if(REMOTE){
      const rows = await sb("docs?coll=eq." + c + "&select=path,data");
      Object.keys(cache).filter(p => collOf(p) === c).forEach(p => delete cache[p]);
      rows.forEach(r => cache[r.path] = r.data);
    } else {
      const s = lsLoad(); Object.keys(cache).filter(p => collOf(p) === c).forEach(p => delete cache[p]);
      Object.keys(s).filter(p => collOf(p) === c).forEach(p => cache[p] = s[p]);
    }
  }
  const allDocs = c => {
    const src = c === "listings" ? staticDocs : cache;
    return Object.keys(src).filter(p => collOf(p) === c).map(p => ({id: idOf(p), data: () => JSON.parse(JSON.stringify(src[p]))}));
  };
  const ready = {};
  const keyOf = l => l.path ? (l.path === "config/status" ? "listings" : collOf(l.path)) : l.coll;
  function emit(){
    listeners.forEach(l => {
      if(!ready[keyOf(l)]) return;
      try {
        if(l.path){ const src = (l.path === "config/status") ? staticDocs : cache; const d = src[l.path];
          l.cb({exists: d !== undefined, data: () => JSON.parse(JSON.stringify(d || {}))}); }
        else l.cb({docs: allDocs(l.coll)});
      } catch(e){ console.warn(e); }
    });
  }
  async function loadStatic(){
    try {
      const [a, b] = await Promise.all([fetch("data/listings.json", {cache: "no-cache"}).then(r => r.json()), fetch("data/status.json", {cache: "no-cache"}).then(r => r.json())]);
      staticDocs = {}; (a.listings || []).forEach(l => { const {id, ...rest} = l; staticDocs["listings/" + id] = rest; });
      staticDocs["config/status"] = b; ready.listings = true; emit();
    } catch(e){ ready.listings = true; listeners.forEach(l => l.err && l.err(e)); }
  }
  async function pollAll(){
    for(const c of sharedColls){ try { await pullColl(c); ready[c] = true; } catch(e){ listeners.forEach(l => l.coll === c && l.err && l.err(e)); } }
    emit();
  }
  let started = false;
  function start(){
    if(started) return; started = true;
    loadStatic(); pollAll();
    setInterval(loadStatic, STATIC_MS); setInterval(pollAll, POLL_MS);
    document.addEventListener("visibilitychange", () => { if(!document.hidden){ pollAll(); loadStatic(); } });
  }

  async function write(path, data){
    cache[path] = data; emit();
    if(REMOTE) await sb("docs?on_conflict=path", {method: "POST", headers: {Prefer: "resolution=merge-duplicates,return=minimal"}, body: JSON.stringify({path, coll: collOf(path), data, updated_at: new Date().toISOString()})});
    else { const s = lsLoad(); s[path] = data; lsSave(s); }
  }
  async function remove(path){
    delete cache[path]; emit();
    if(REMOTE) await sb("docs?path=eq." + encodeURIComponent(path), {method: "DELETE"});
    else { const s = lsLoad(); delete s[path]; lsSave(s); }
  }
  const isObj = v => v && typeof v === "object" && !Array.isArray(v);
  function mergeInto(base, patch){
    const out = {...base};
    Object.keys(patch).forEach(k => {
      const v = patch[k];
      if(isObj(v) && isObj(out[k])){ const inner = {...out[k]}; Object.keys(v).forEach(kk => { if(isObj(v[kk]) && v[kk].__delete__) delete inner[kk]; else inner[kk] = v[kk]; }); out[k] = inner; }
      else out[k] = v;
    });
    return out;
  }
  const docRef = path => ({
    set: async d => write(path, JSON.parse(JSON.stringify(d))),
    update: async d => { if(REMOTE) await pullColl(collOf(path)); await write(path, mergeInto(cache[path] || {}, d)); },
    delete: async () => remove(path),
    onSnapshot: (cb, err) => { listeners.push({path, cb, err}); start(); emit(); }
  });
  const db = {
    doc: docRef,
    collection: c => ({
      onSnapshot: (cb, err) => { listeners.push({coll: c, cb, err}); start(); emit(); },
      add: async d => write(c + "/" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), JSON.parse(JSON.stringify(d)))
    })
  };
  window.claude = { use: async n => n === "user" ? userCap : n === "db" ? db : null };
  window.HH = { userCap, remote: REMOTE };
})();
