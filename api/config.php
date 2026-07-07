<?php
/**
 * ROBLOX MAP ARCHITECT — Configuração
 */

// ─── Banco de dados ───
define('DB_HOST', 'localhost');
define('DB_NAME', 'techobco_roblox');
define('DB_USER', 'techobco_roblox');
define('DB_PASS', '@Precisao2026');
define('DB_CHARSET', 'utf8mb4');

// ─── App ───
define('APP_NAME', 'Roblox Map Architect');
define('APP_VERSION', '1.0.0');
define('APP_URL', 'https://precisao.agr.br/apis');

// ─── CORS — Domínios autorizados a acessar a API ───
define('CORS_ORIGINS', [
    'https://precisao.agr.br',                    // Mesmo domínio
    'http://precisao.agr.br',                     // HTTP fallback
    'https://roblox-map-architect.netlify.app',    // ← Trocar pelo link real do Netlify
    'http://localhost',                            // Dev local
    'http://127.0.0.1',                            // Dev local
]);

// ─── Paths ───
define('UPLOAD_DIR', __DIR__ . '/uploads/');
define('HEIGHTMAP_DIR', UPLOAD_DIR . 'heightmaps/');
define('COLORMAP_DIR', UPLOAD_DIR . 'colormaps/');
define('THUMBNAIL_DIR', UPLOAD_DIR . 'thumbnails/');

// ─── Limites ───
define('MAX_MAP_SIZE', 4096);       // max studs
define('MAX_MAPS_PER_SESSION', 50); // limite por sessão
define('MAX_PRESETS_USER', 20);     // presets custom por sessão

// Criar diretórios se não existem
foreach ([UPLOAD_DIR, HEIGHTMAP_DIR, COLORMAP_DIR, THUMBNAIL_DIR] as $dir) {
    if (!is_dir($dir)) {
        mkdir($dir, 0755, true);
    }
}
