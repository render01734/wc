"""
⚠️  DEPRECATED — Bu dosya v10.0'da geçersizdir
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Bu patch artık KULLANILMAMAKTADIR ve mc_panel.py'e EKLENMEMELIDIR.

NEDEN:
  Bu dosyadaki tüm route'lar mc_panel.py'de zaten native olarak
  tanımlıdır. Eklenmesi Flask'ta AssertionError (duplicate route) verir:

    Duplicate route: /api/agent/register   → zaten mc_panel.py'de var
    Duplicate route: /api/agent/heartbeat  → zaten mc_panel.py'de var
    Duplicate route: /api/pool/status      → zaten mc_panel.py'de var
    Duplicate route: /api/pool/cache/flush → zaten mc_panel.py'de var
    Duplicate route: /api/pool/regions     → zaten mc_panel.py'de var
    Duplicate route: /api/pool/proxy/start → zaten mc_panel.py'de var
    Duplicate route: /api/pool/proxy/stop  → zaten mc_panel.py'de var
    Duplicate route: /api/pool/task        → zaten mc_panel.py'de var

AYRICA:
  Bu dosya resource_pool.py'den 'pool' import ediyordu ama
  mc_panel.py'nin eski inline pool'u kullanmıyordu.
  v10.0'da mc_panel.py doğrudan resource_pool.pool'u kullanır
  (_pool takma adıyla) — bu patch'e gerek yoktur.

YAPILMASI GEREKEN:
  Bu dosyayı repoda tutabilirsiniz (tarihsel belge olarak)
  ama mc_panel.py'e eklemeyin.
"""

# Bu dosyadaki kod bloğu artık geçersizdir.
# Tüm işlevsellik mc_panel.py + resource_pool.py içindedir.
