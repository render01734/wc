"""
mc_panel.py'e eklenecek kod bloğu
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bu dosyayı mc_panel.py'deki şu satırın HEMEN ALTINA yapıştır:
  @app.route("/api/worker/status")
  def api_worker_status():
      ...
      return jsonify({"nodes": list(support_nodes.values())})
"""

# ── Resource Agent API ───────────────────────────────────────

@app.route("/api/agent/register", methods=["POST"])
def api_agent_register():
    """agent.py'den gelen kayıt isteği."""
    from resource_pool import pool
    d = request.json or {}
    tunnel_url = d.get("tunnel", "")
    node_id    = d.get("node_id", "")
    if not tunnel_url or not node_id:
        return jsonify({"ok": False, "error": "tunnel veya node_id eksik"})
    pool.set_logger(log)
    pool.register(tunnel_url, node_id, d)
    log(f"[Pool] ✅ Agent bağlandı: {node_id} | "
        f"RAM:{d.get('ram',{}).get('free_mb',0)}MB boş | "
        f"Disk:{d.get('disk',{}).get('free_gb',0)}GB boş")
    socketio.emit("pool_update", pool.summary())
    return jsonify({"ok": True, "message": f"Agent {node_id} kaydedildi"})


@app.route("/api/agent/heartbeat", methods=["POST"])
def api_agent_heartbeat():
    from resource_pool import pool
    d = request.json or {}
    node_id    = d.get("node_id", "")
    tunnel_url = d.get("tunnel", "")
    if node_id and tunnel_url:
        pool.register(tunnel_url, node_id, d)
    socketio.emit("pool_update", pool.summary())
    return jsonify({"ok": True})


@app.route("/api/pool/status")
def api_pool_status():
    from resource_pool import pool
    return jsonify(pool.summary())


@app.route("/api/pool/cache/flush", methods=["POST"])
def api_pool_cache_flush():
    from resource_pool import pool
    prefix = (request.json or {}).get("prefix", "")
    n = pool.cache_flush_all(prefix)
    log(f"[Pool] 🗑️  {n} önbellek anahtarı temizlendi")
    return jsonify({"ok": True, "flushed": n})


@app.route("/api/pool/regions")
def api_pool_regions():
    from resource_pool import pool
    dim = request.args.get("dim", "world")
    regions = pool.list_remote_regions(dim)
    return jsonify({"regions": regions, "count": len(regions)})


@app.route("/api/pool/proxy/start", methods=["POST"])
def api_pool_proxy_start():
    from resource_pool import pool
    d = request.json or {}
    mc_host = d.get("host", "127.0.0.1")
    mc_port = int(d.get("port", 25565))
    started = pool.start_proxies(mc_host, mc_port)
    log(f"[Pool] 🔀 {len(started)} agent'ta proxy başlatıldı")
    return jsonify({"ok": True, "started": started})


@app.route("/api/pool/proxy/stop", methods=["POST"])
def api_pool_proxy_stop():
    from resource_pool import pool
    pool.stop_proxies()
    return jsonify({"ok": True})


@app.route("/api/pool/task", methods=["POST"])
def api_pool_task():
    from resource_pool import pool
    d = request.json or {}
    result = pool.run_task(
        d.get("type", "echo"),
        d.get("payload", {}),
        wait=d.get("wait", True),
        timeout=d.get("timeout", 30),
    )
    return jsonify({"ok": result is not None, "result": result})
