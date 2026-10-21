"""Clearly labelled sample data; never mistaken for a production scan."""

from .model import utcnow


def sample():
    sites = []
    for i, domain in enumerate(("adventistai.lt", "dievozodis.lt", "malonetau.lt", "sveikas.info", "tavoseimai.lt", "giesmynas.adventistai.lt", "vilnius.adventistai.lt")):
        wp = i < 5
        plugins = [{"name": "wordfence", "status": "active", "version": "8.0.0",
                    "update_version": "8.1.0" if i in (0, 2) else None, "source": "wordpress.org"}]
        if i == 0:
            plugins.append({"name": "knygos-igarsinimo-registracija", "status": "active", "version": "2.4.1", "update_version": "2.4.2", "source": "github", "repository": "example/registration"})
        sites.append({"id": domain, "kind": "wordpress" if wp else "web", "state": "partial" if i == 3 else "ok",
                      "core": {"version": "6.8.2", "update_version": "6.8.3" if i == 1 else None} if wp else None,
                      "plugins": plugins if wp else [], "themes": [{"name": "adventistai-alps", "status": "active", "version": "3.0.0", "update_version": "3.1.0", "source": "github"}] if i == 0 else [],
                      "flags": {"DISALLOW_FILE_MODS": True, "DISALLOW_FILE_EDIT": True, "WP_DEBUG": False} if wp else {},
                      "health": [{"path": "/", "status": 200, "response_ms": 145 + i * 21, "ok": True}, {"path": "/wp-login.php", "status": 200, "response_ms": 213, "ok": True}] if wp else [{"path": "/", "status": 200, "response_ms": 95, "ok": True}],
                      "backup": {"state": "not_configured"}, "errors": ["Theme inventory unavailable (sample)"] if i == 3 else [],
                      "notes": ["Sample data only"]})
    return {"schema_version": 1, "collected_at": utcnow(), "demo": True,
            "server": {"id": "demo-server", "hostname": "Server preview", "php_version": "8.4.0",
                       "disk": {"total_bytes": 53687091200, "used_bytes": 19327352832, "free_bytes": 34359738368},
                       "memory": {"total_bytes": 4294967296, "available_bytes": 1717986918},
                       "load": [0.48, 0.62, 0.51], "failed_units": [], "errors": []}, "sites": sites}
