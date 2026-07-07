<?php
/**
 * ROBLOX MAP ARCHITECT — API Principal
 * 
 * Endpoints:
 *   GET    /api/?action=presets           → Listar presets
 *   GET    /api/?action=preset&id=X       → Obter preset específico
 *   POST   /api/?action=preset_save       → Salvar preset custom
 *   DELETE /api/?action=preset_delete&id=X → Deletar preset
 * 
 *   POST   /api/?action=generate          → Gerar mapa (salva config + gera Lua)
 *   GET    /api/?action=maps              → Listar mapas gerados
 *   GET    /api/?action=map&id=X          → Obter mapa com script
 *   DELETE /api/?action=map_delete&id=X   → Deletar mapa
 * 
 *   GET    /api/?action=materials         → Listar materiais de terreno
 *   GET    /api/?action=biomes            → Listar configs de biomas
 *   GET    /api/?action=assets            → Listar biblioteca de assets
 *   POST   /api/?action=asset_save        → Salvar asset custom
 * 
 *   GET    /api/?action=stats             → Estatísticas gerais
 *   POST   /api/?action=install           → Executar schema SQL (primeira vez)
 */

header('Content-Type: application/json; charset=utf-8');

// ─── CORS seguro: só aceita origens autorizadas no config.php ───
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if (defined('CORS_ORIGINS') && in_array($origin, CORS_ORIGINS)) {
    header('Access-Control-Allow-Origin: ' . $origin);
} else {
    // Fallback: aceita tudo durante desenvolvimento
    // Em produção, remova esta linha e use apenas o CORS_ORIGINS
    header('Access-Control-Allow-Origin: *');
}
header('Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

require_once __DIR__ . '/config.php';
require_once __DIR__ . '/Database.php';
require_once __DIR__ . '/LuaGenerator.php';

// ─── Session ID (identificador do usuário sem login) ───
function getSessionId(): string
{
    if (session_status() === PHP_SESSION_NONE) {
        session_start();
    }
    return session_id();
}

function getClientIP(): string
{
    return $_SERVER['HTTP_X_FORWARDED_FOR']
        ?? $_SERVER['HTTP_CLIENT_IP']
        ?? $_SERVER['REMOTE_ADDR']
        ?? '0.0.0.0';
}

function getInput(): array
{
    $json = json_decode(file_get_contents('php://input'), true);
    return $json ?? $_POST;
}

function respond(mixed $data, int $code = 200): never
{
    http_response_code($code);
    echo json_encode(['ok' => $code < 400, 'data' => $data], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    exit;
}

function respondError(string $message, int $code = 400): never
{
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $message], JSON_UNESCAPED_UNICODE);
    exit;
}

function logAction(string $action, ?int $mapId = null, ?array $details = null): void
{
    try {
        Database::insert('generation_log', [
            'map_id'     => $mapId,
            'action'     => $action,
            'details'    => $details ? json_encode($details) : null,
            'session_id' => getSessionId(),
            'ip_address' => getClientIP(),
        ]);
    } catch (Exception $e) {
        // silenciar erros de log
    }
}

// ═══════════════════════════════════════════════════════
//  ROUTER
// ═══════════════════════════════════════════════════════

$action = $_GET['action'] ?? '';

try {
    match ($action) {
        // ─── Presets ───
        'presets'        => handleGetPresets(),
        'preset'         => handleGetPreset(),
        'preset_save'    => handleSavePreset(),
        'preset_delete'  => handleDeletePreset(),

        // ─── Mapas ───
        'generate'       => handleGenerate(),
        'maps'           => handleGetMaps(),
        'map'            => handleGetMap(),
        'map_delete'     => handleDeleteMap(),

        // ─── Referência ───
        'materials'      => handleGetMaterials(),
        'biomes'         => handleGetBiomes(),
        'assets'         => handleGetAssets(),
        'asset_save'     => handleSaveAsset(),

        // ─── Sistema ───
        'stats'          => handleStats(),
        'install'        => handleInstall(),

        default => respondError("Ação desconhecida: $action", 404),
    };
} catch (PDOException $e) {
    respondError('Erro de banco de dados: ' . $e->getMessage(), 500);
} catch (Exception $e) {
    respondError($e->getMessage(), 500);
}


// ═══════════════════════════════════════════════════════
//  HANDLERS — PRESETS
// ═══════════════════════════════════════════════════════

function handleGetPresets(): void
{
    $presets = Database::fetchAll(
        "SELECT id, slug, name, icon, description, is_system, is_public, use_count, 
                config_json, thumbnail, created_at 
         FROM presets 
         WHERE is_public = 1 OR session_id = ?
         ORDER BY is_system DESC, use_count DESC",
        // presets do sistema não têm session_id, então usamos OR
    );

    // Fallback sem session_id na query (presets são públicos)
    $presets = Database::fetchAll(
        "SELECT id, slug, name, icon, description, is_system, is_public, use_count, 
                config_json, thumbnail, created_at 
         FROM presets 
         WHERE is_public = 1
         ORDER BY is_system DESC, use_count DESC"
    );

    // Decodificar config_json
    foreach ($presets as &$p) {
        $p['config'] = json_decode($p['config_json'], true);
        unset($p['config_json']);
    }
    respond($presets);
}

function handleGetPreset(): void
{
    $id = (int) ($_GET['id'] ?? 0);
    if (!$id) respondError('ID obrigatório');

    $preset = Database::fetchOne("SELECT * FROM presets WHERE id = ?", [$id]);
    if (!$preset) respondError('Preset não encontrado', 404);

    $preset['config'] = json_decode($preset['config_json'], true);
    unset($preset['config_json']);

    // Incrementar contador de uso
    Database::query("UPDATE presets SET use_count = use_count + 1 WHERE id = ?", [$id]);

    respond($preset);
}

function handleSavePreset(): void
{
    $input = getInput();

    $name   = trim($input['name'] ?? '');
    $config = $input['config'] ?? null;
    $desc   = trim($input['description'] ?? '');
    $icon   = trim($input['icon'] ?? '🗺️');

    if (!$name) respondError('Nome obrigatório');
    if (!$config) respondError('Configuração obrigatória');

    $slug = preg_replace('/[^a-z0-9]+/', '-', strtolower($name));
    $slug = trim($slug, '-');

    // Verificar se já existe
    $existing = Database::fetchOne("SELECT id FROM presets WHERE slug = ?", [$slug]);

    if ($existing) {
        Database::update('presets', $existing['id'], [
            'name'        => $name,
            'icon'        => $icon,
            'description' => $desc,
            'config_json' => json_encode($config),
        ]);
        $id = $existing['id'];
    } else {
        $id = Database::insert('presets', [
            'slug'        => $slug,
            'name'        => $name,
            'icon'        => $icon,
            'description' => $desc,
            'config_json' => json_encode($config),
            'is_system'   => 0,
            'is_public'   => 1,
        ]);
    }

    logAction('preset_saved', null, ['preset_id' => $id, 'name' => $name]);
    respond(['id' => $id, 'slug' => $slug, 'message' => 'Preset salvo']);
}

function handleDeletePreset(): void
{
    $id = (int) ($_GET['id'] ?? 0);
    if (!$id) respondError('ID obrigatório');

    // Não deletar presets do sistema
    $preset = Database::fetchOne("SELECT is_system FROM presets WHERE id = ?", [$id]);
    if (!$preset) respondError('Preset não encontrado', 404);
    if ($preset['is_system']) respondError('Não é possível deletar presets do sistema');

    Database::delete('presets', $id);
    logAction('preset_deleted', null, ['preset_id' => $id]);
    respond(['message' => 'Preset deletado']);
}


// ═══════════════════════════════════════════════════════
//  HANDLERS — GERAÇÃO DE MAPAS
// ═══════════════════════════════════════════════════════

function handleGenerate(): void
{
    $input  = getInput();
    $config = $input['config'] ?? null;
    $name   = trim($input['name'] ?? 'Mapa ' . date('d/m H:i'));

    if (!$config) respondError('Configuração obrigatória');

    $startTime = microtime(true);

    // Gerar UUID
    $uuid = sprintf('%04x%04x-%04x-%04x-%04x-%04x%04x%04x',
        mt_rand(0, 0xffff), mt_rand(0, 0xffff), mt_rand(0, 0xffff),
        mt_rand(0, 0x0fff) | 0x4000,
        mt_rand(0, 0x3fff) | 0x8000,
        mt_rand(0, 0xffff), mt_rand(0, 0xffff), mt_rand(0, 0xffff)
    );

    // Calcular tamanhos
    $sizeMap = [
        'tiny' => [256, 64], 'small' => [512, 128], 'medium' => [1024, 256],
        'large' => [2048, 512], 'epic' => [4096, 1024],
    ];
    $sz = $sizeMap[$config['size'] ?? 'medium'] ?? [1024, 256];

    // Gerar script Lua
    $luaGen = new LuaGenerator($config);
    $luaScript = $luaGen->generate();

    // Determinar biomas usados
    $biomes = match ($config['preset'] ?? 'custom') {
        'tropical' => 'JUNGLE,PLAINS',
        'medieval' => 'FOREST,PLAINS,SWAMP',
        'volcanic' => 'VOLCANIC',
        'arctic'   => 'ARCTIC,TUNDRA',
        'desert'   => 'DESERT',
        'skylands' => 'FLOATING',
        default    => 'CUSTOM',
    };

    $genTime = (int) ((microtime(true) - $startTime) * 1000);

    // Salvar no banco
    $mapId = Database::insert('generated_maps', [
        'uuid'              => $uuid,
        'name'              => $name,
        'preset_id'         => $input['preset_id'] ?? null,
        'config_json'       => json_encode($config),
        'lua_script'        => $luaScript,
        'size_studs'        => $sz[0],
        'size_voxels'       => $sz[1],
        'biomes_used'       => $biomes,
        'generation_time_ms'=> $genTime,
        'status'            => 'completed',
        'session_id'        => getSessionId(),
        'ip_address'        => getClientIP(),
    ]);

    logAction('map_generated', $mapId, [
        'preset' => $config['preset'] ?? 'custom',
        'size'   => $config['size'] ?? 'medium',
        'seed'   => $config['seed'] ?? 0,
    ]);

    respond([
        'id'         => $mapId,
        'uuid'       => $uuid,
        'name'       => $name,
        'lua_script' => $luaScript,
        'size_studs' => $sz[0],
        'biomes'     => $biomes,
        'gen_time_ms'=> $genTime,
        'message'    => 'Mapa gerado com sucesso',
    ]);
}

function handleGetMaps(): void
{
    $page  = max(1, (int) ($_GET['page'] ?? 1));
    $limit = min(50, max(5, (int) ($_GET['limit'] ?? 20)));
    $offset = ($page - 1) * $limit;

    $total = Database::fetchOne("SELECT COUNT(*) as cnt FROM generated_maps")['cnt'];

    $maps = Database::fetchAll(
        "SELECT id, uuid, name, preset_id, size_studs, size_voxels, biomes_used,
                generation_time_ms, status, created_at
         FROM generated_maps
         ORDER BY created_at DESC
         LIMIT ? OFFSET ?",
        [$limit, $offset]
    );

    respond([
        'maps'       => $maps,
        'total'      => (int) $total,
        'page'       => $page,
        'pages'      => ceil($total / $limit),
        'per_page'   => $limit,
    ]);
}

function handleGetMap(): void
{
    $id = (int) ($_GET['id'] ?? 0);
    $uuid = $_GET['uuid'] ?? '';

    if ($id) {
        $map = Database::fetchOne("SELECT * FROM generated_maps WHERE id = ?", [$id]);
    } elseif ($uuid) {
        $map = Database::fetchOne("SELECT * FROM generated_maps WHERE uuid = ?", [$uuid]);
    } else {
        respondError('ID ou UUID obrigatório');
    }

    if (!$map) respondError('Mapa não encontrado', 404);

    $map['config'] = json_decode($map['config_json'], true);
    unset($map['config_json']);

    respond($map);
}

function handleDeleteMap(): void
{
    $id = (int) ($_GET['id'] ?? 0);
    if (!$id) respondError('ID obrigatório');

    Database::delete('generated_maps', $id);
    logAction('map_deleted', $id);
    respond(['message' => 'Mapa deletado']);
}


// ═══════════════════════════════════════════════════════
//  HANDLERS — REFERÊNCIA
// ═══════════════════════════════════════════════════════

function handleGetMaterials(): void
{
    $materials = Database::fetchAll("SELECT * FROM terrain_materials ORDER BY category, enum_name");
    respond($materials);
}

function handleGetBiomes(): void
{
    $biomes = Database::fetchAll("SELECT * FROM biome_configs ORDER BY name");
    respond($biomes);
}

function handleGetAssets(): void
{
    $category = $_GET['category'] ?? null;
    $biome    = $_GET['biome'] ?? null;

    $sql = "SELECT * FROM asset_library WHERE is_active = 1";
    $params = [];

    if ($category) {
        $sql .= " AND category = ?";
        $params[] = $category;
    }
    if ($biome) {
        $sql .= " AND (biomes LIKE ? OR biomes IS NULL)";
        $params[] = "%$biome%";
    }

    $sql .= " ORDER BY category, name";
    respond(Database::fetchAll($sql, $params));
}

function handleSaveAsset(): void
{
    $input = getInput();
    $name     = trim($input['name'] ?? '');
    $category = $input['category'] ?? '';

    if (!$name) respondError('Nome obrigatório');
    if (!$category) respondError('Categoria obrigatória');

    $id = Database::insert('asset_library', [
        'name'            => $name,
        'category'        => $category,
        'subcategory'     => $input['subcategory'] ?? null,
        'roblox_asset_id' => $input['roblox_asset_id'] ?? null,
        'model_json'      => isset($input['model_json']) ? json_encode($input['model_json']) : null,
        'lua_code'        => $input['lua_code'] ?? null,
        'biomes'          => $input['biomes'] ?? null,
        'density_default' => $input['density'] ?? 0.02,
        'min_height'      => $input['min_height'] ?? 0,
        'max_height'      => $input['max_height'] ?? 1,
    ]);

    logAction('asset_saved', null, ['asset_id' => $id, 'name' => $name]);
    respond(['id' => $id, 'message' => 'Asset salvo']);
}


// ═══════════════════════════════════════════════════════
//  HANDLERS — SISTEMA
// ═══════════════════════════════════════════════════════

function handleStats(): void
{
    $stats = [
        'total_maps'    => (int) Database::fetchOne("SELECT COUNT(*) as c FROM generated_maps")['c'],
        'total_presets'  => (int) Database::fetchOne("SELECT COUNT(*) as c FROM presets")['c'],
        'total_assets'   => (int) Database::fetchOne("SELECT COUNT(*) as c FROM asset_library WHERE is_active=1")['c'],
        'total_materials'=> (int) Database::fetchOne("SELECT COUNT(*) as c FROM terrain_materials")['c'],
        'recent_maps'    => Database::fetchAll(
            "SELECT id, name, size_studs, biomes_used, created_at 
             FROM generated_maps ORDER BY created_at DESC LIMIT 5"
        ),
        'popular_presets' => Database::fetchAll(
            "SELECT id, name, icon, use_count 
             FROM presets ORDER BY use_count DESC LIMIT 5"
        ),
    ];
    respond($stats);
}

function handleInstall(): void
{
    $sqlFile = __DIR__ . '/../sql/schema.sql';
    if (!file_exists($sqlFile)) {
        respondError('Arquivo schema.sql não encontrado');
    }

    $sql = file_get_contents($sqlFile);
    $pdo = Database::get();

    // Executar cada statement separadamente
    $statements = array_filter(
        array_map('trim', explode(';', $sql)),
        fn($s) => !empty($s) && !str_starts_with($s, '--')
    );

    $executed = 0;
    $errors = [];

    foreach ($statements as $stmt) {
        try {
            $pdo->exec($stmt);
            $executed++;
        } catch (PDOException $e) {
            // Ignorar erros de "já existe"
            if (strpos($e->getMessage(), 'already exists') === false &&
                strpos($e->getMessage(), 'Duplicate') === false) {
                $errors[] = substr($e->getMessage(), 0, 100);
            }
        }
    }

    logAction('system_install', null, ['executed' => $executed, 'errors' => count($errors)]);

    respond([
        'message'    => 'Instalação concluída',
        'statements' => $executed,
        'errors'     => $errors,
    ]);
}
