-- ═══════════════════════════════════════════════════════
-- ROBLOX MAP ARCHITECT — Database Schema
-- Base: techobco_roblox
-- ═══════════════════════════════════════════════════════

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ─── Presets (templates do sistema) ───
CREATE TABLE IF NOT EXISTS `presets` (
  `id` INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `slug` VARCHAR(50) NOT NULL UNIQUE,
  `name` VARCHAR(100) NOT NULL,
  `icon` VARCHAR(10) DEFAULT '🗺️',
  `description` TEXT,
  `config_json` JSON NOT NULL,
  `thumbnail` VARCHAR(255) DEFAULT NULL,
  `is_system` TINYINT(1) DEFAULT 0 COMMENT 'Presets padrão do sistema',
  `is_public` TINYINT(1) DEFAULT 1,
  `use_count` INT UNSIGNED DEFAULT 0,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX `idx_public` (`is_public`),
  INDEX `idx_popular` (`use_count` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── Mapas gerados ───
CREATE TABLE IF NOT EXISTS `generated_maps` (
  `id` INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `uuid` CHAR(36) NOT NULL UNIQUE,
  `name` VARCHAR(150) DEFAULT 'Mapa sem nome',
  `preset_id` INT UNSIGNED DEFAULT NULL,
  `config_json` JSON NOT NULL COMMENT 'Snapshot completo da config no momento da geração',
  `lua_script` LONGTEXT COMMENT 'Script Lua gerado',
  `python_script` TEXT COMMENT 'Script Python (heightmap) se aplicável',
  `heightmap_path` VARCHAR(255) DEFAULT NULL,
  `colormap_path` VARCHAR(255) DEFAULT NULL,
  `size_studs` INT UNSIGNED DEFAULT 0,
  `size_voxels` INT UNSIGNED DEFAULT 0,
  `biomes_used` VARCHAR(255) DEFAULT NULL,
  `generation_time_ms` INT UNSIGNED DEFAULT 0,
  `status` ENUM('pending','generating','completed','failed') DEFAULT 'completed',
  `session_id` VARCHAR(64) DEFAULT NULL COMMENT 'Identificador de sessão do usuário',
  `ip_address` VARCHAR(45) DEFAULT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX `idx_session` (`session_id`),
  INDEX `idx_status` (`status`),
  INDEX `idx_created` (`created_at` DESC),
  FOREIGN KEY (`preset_id`) REFERENCES `presets`(`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── Biblioteca de assets ───
CREATE TABLE IF NOT EXISTS `asset_library` (
  `id` INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `name` VARCHAR(100) NOT NULL,
  `category` ENUM('tree','rock','structure','vegetation','prop','effect','custom') NOT NULL,
  `subcategory` VARCHAR(50) DEFAULT NULL,
  `roblox_asset_id` BIGINT UNSIGNED DEFAULT NULL COMMENT 'Asset ID do Creator Store',
  `model_json` JSON DEFAULT NULL COMMENT 'Definição Rojo model.json',
  `lua_code` TEXT DEFAULT NULL COMMENT 'Código Lua para gerar proceduralmente',
  `thumbnail` VARCHAR(255) DEFAULT NULL,
  `biomes` VARCHAR(255) DEFAULT NULL COMMENT 'Biomas compatíveis separados por vírgula',
  `min_height` FLOAT DEFAULT 0 COMMENT 'Altura mínima para placement (normalizada 0-1)',
  `max_height` FLOAT DEFAULT 1 COMMENT 'Altura máxima para placement',
  `density_default` FLOAT DEFAULT 0.02,
  `scale_min` FLOAT DEFAULT 0.8,
  `scale_max` FLOAT DEFAULT 1.2,
  `anchored` TINYINT(1) DEFAULT 1,
  `is_active` TINYINT(1) DEFAULT 1,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX `idx_category` (`category`),
  INDEX `idx_biomes` (`biomes`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── Materiais de terreno (referência) ───
CREATE TABLE IF NOT EXISTS `terrain_materials` (
  `id` INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `enum_name` VARCHAR(30) NOT NULL UNIQUE,
  `label_pt` VARCHAR(50) NOT NULL,
  `rgb_r` TINYINT UNSIGNED NOT NULL,
  `rgb_g` TINYINT UNSIGNED NOT NULL,
  `rgb_b` TINYINT UNSIGNED NOT NULL,
  `hex_color` CHAR(7) NOT NULL,
  `category` ENUM('natural','rock','snow_ice','urban','special') DEFAULT 'natural',
  `typical_use` VARCHAR(100) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── Configurações de bioma ───
CREATE TABLE IF NOT EXISTS `biome_configs` (
  `id` INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `name` VARCHAR(50) NOT NULL UNIQUE,
  `label_pt` VARCHAR(50) NOT NULL,
  `base_material` VARCHAR(30) NOT NULL,
  `amplitude_factor` FLOAT DEFAULT 1.0,
  `tree_density` FLOAT DEFAULT 0.02,
  `rock_density` FLOAT DEFAULT 0.005,
  `water_level_offset` FLOAT DEFAULT 0,
  `temperature_min` FLOAT DEFAULT 0,
  `temperature_max` FLOAT DEFAULT 1,
  `moisture_min` FLOAT DEFAULT 0,
  `moisture_max` FLOAT DEFAULT 1,
  `slope_materials` JSON DEFAULT NULL COMMENT '{"low":"Grass","mid":"Ground","high":"Rock"}',
  `supports_trees` TINYINT(1) DEFAULT 1,
  `supports_caves` TINYINT(1) DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ─── Histórico / Log de uso ───
CREATE TABLE IF NOT EXISTS `generation_log` (
  `id` BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  `map_id` INT UNSIGNED DEFAULT NULL,
  `action` VARCHAR(50) NOT NULL,
  `details` JSON DEFAULT NULL,
  `session_id` VARCHAR(64) DEFAULT NULL,
  `ip_address` VARCHAR(45) DEFAULT NULL,
  `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX `idx_action` (`action`),
  INDEX `idx_session_log` (`session_id`),
  FOREIGN KEY (`map_id`) REFERENCES `generated_maps`(`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ═══════════════════════════════════════════════════════
--  DADOS INICIAIS
-- ═══════════════════════════════════════════════════════

-- Materiais de Terreno (cores RGB exatas do Roblox)
INSERT INTO `terrain_materials` (`enum_name`, `label_pt`, `rgb_r`, `rgb_g`, `rgb_b`, `hex_color`, `category`, `typical_use`) VALUES
('Grass',       'Grama',        106, 127, 63,  '#6A7F3F', 'natural',   'Planícies, campos'),
('LeafyGrass',  'Grama Densa',  115, 132, 74,  '#73844A', 'natural',   'Florestas, vegetação densa'),
('Sand',        'Areia',        143, 126, 95,  '#8F7E5F', 'natural',   'Praias, desertos'),
('Rock',        'Rocha',        102, 108, 111, '#666C6F', 'rock',      'Montanhas, penhascos'),
('Slate',       'Ardósia',      63,  127, 107, '#3F7F6B', 'rock',      'Encostas, formações'),
('Snow',        'Neve',         195, 199, 218, '#C3C7DA', 'snow_ice',  'Picos, regiões árticas'),
('Ice',         'Gelo',         129, 194, 224, '#81C2E0', 'snow_ice',  'Geleiras, lagos congelados'),
('Glacier',     'Geleira',      101, 176, 234, '#65B0EA', 'snow_ice',  'Massas glaciais'),
('Mud',         'Lama',         58,  46,  36,  '#3A2E24', 'natural',   'Pântanos, áreas úmidas'),
('Ground',      'Terra',        102, 92,  59,  '#665C3B', 'natural',   'Terra batida, caminhos'),
('Sandstone',   'Arenito',      137, 90,  71,  '#895A47', 'rock',      'Canyons, mesas'),
('Limestone',   'Calcário',     206, 173, 148, '#CEAD94', 'rock',      'Formações calcárias'),
('Basalt',      'Basalto',      80,  80,  80,  '#505050', 'rock',      'Regiões vulcânicas'),
('CrackedLava', 'Lava',         232, 156, 74,  '#E89C4A', 'special',   'Vulcões ativos'),
('Salt',        'Sal',          198, 189, 181, '#C6BDB5', 'natural',   'Salares'),
('Water',       'Água',         12,  84,  92,  '#0C545C', 'special',   'Oceanos, rios, lagos'),
('Concrete',    'Concreto',     127, 127, 127, '#7F7F7F', 'urban',     'Áreas urbanas'),
('Pavement',    'Pavimento',    148, 148, 140, '#94948C', 'urban',     'Estradas, calçadas'),
('Cobblestone', 'Paralelepípedo',132,132, 132, '#848484', 'urban',     'Caminhos medievais'),
('WoodPlanks',  'Madeira',      139, 109, 79,  '#8B6D4F', 'natural',   'Decks, pontes');

-- Biomas padrão
INSERT INTO `biome_configs` (`name`, `label_pt`, `base_material`, `amplitude_factor`, `tree_density`, `rock_density`, `temperature_min`, `temperature_max`, `moisture_min`, `moisture_max`, `supports_trees`) VALUES
('PLAINS',  'Planícies', 'Grass',      0.5, 0.005, 0.003, 0.4, 0.7, 0.0, 0.3, 0),
('FOREST',  'Floresta',  'Grass',      0.7, 0.10,  0.005, 0.4, 0.7, 0.3, 0.6, 1),
('JUNGLE',  'Selva',     'LeafyGrass', 0.8, 0.15,  0.003, 0.7, 1.0, 0.5, 1.0, 1),
('DESERT',  'Deserto',   'Sand',       0.4, 0.001, 0.002, 0.7, 1.0, 0.0, 0.5, 0),
('SWAMP',   'Pântano',   'Mud',        0.3, 0.05,  0.001, 0.4, 0.7, 0.6, 1.0, 1),
('TUNDRA',  'Tundra',    'Snow',       0.5, 0.002, 0.005, 0.0, 0.4, 0.5, 1.0, 0),
('ARCTIC',  'Ártico',    'Ice',        0.6, 0.000, 0.005, 0.0, 0.4, 0.0, 0.5, 0),
('VOLCANIC','Vulcânico',  'Basalt',     0.9, 0.000, 0.010, 0.7, 1.0, 0.0, 0.3, 0);

-- Presets do sistema
INSERT INTO `presets` (`slug`, `name`, `icon`, `description`, `is_system`, `config_json`) VALUES
('tropical', 'Ilha Tropical', '🏝️', 'Ilha com praias, montanha central e vegetação tropical', 1, '{"preset":"tropical","size":"medium","seed":42,"scale":0.008,"octaves":6,"lacunarity":2,"gain":0.5,"waterLevel":0.3,"amplitude":0.7,"mountainThreshold":0.6,"mountainAmplitude":0.4,"cavesEnabled":false,"rivers":true,"volcano":false,"islandMode":true,"placeTrees":true,"treeDensity":0.02,"placeRocks":true,"rockDensity":0.005,"lighting":"tropical"}'),
('medieval', 'Mundo Medieval', '🏰', 'Continente com florestas densas, rios e cavernas', 1, '{"preset":"medieval","size":"medium","seed":42,"scale":0.008,"octaves":6,"lacunarity":2,"gain":0.5,"waterLevel":0.25,"amplitude":0.6,"mountainThreshold":0.6,"mountainAmplitude":0.4,"cavesEnabled":true,"rivers":true,"volcano":false,"islandMode":false,"placeTrees":true,"treeDensity":0.03,"placeRocks":true,"rockDensity":0.008,"lighting":"medieval"}'),
('volcanic', 'Vulcão Apocalíptico', '🌋', 'Ilha vulcânica com lava, basalto e cratera', 1, '{"preset":"volcanic","size":"medium","seed":42,"scale":0.008,"octaves":6,"lacunarity":2,"gain":0.5,"waterLevel":0.1,"amplitude":0.9,"mountainThreshold":0.5,"mountainAmplitude":0.6,"cavesEnabled":true,"rivers":false,"volcano":true,"islandMode":true,"placeTrees":false,"treeDensity":0,"placeRocks":true,"rockDensity":0.01,"lighting":"volcanic"}'),
('arctic', 'Ártico Gelado', '❄️', 'Paisagem congelada com geleiras e picos nevados', 1, '{"preset":"arctic","size":"medium","seed":42,"scale":0.008,"octaves":6,"lacunarity":2,"gain":0.5,"waterLevel":0.2,"amplitude":0.65,"mountainThreshold":0.6,"mountainAmplitude":0.4,"cavesEnabled":false,"rivers":false,"volcano":false,"islandMode":false,"placeTrees":false,"treeDensity":0,"placeRocks":true,"rockDensity":0.005,"lighting":"arctic"}'),
('desert', 'Deserto com Oásis', '🏜️', 'Dunas de areia, mesas de arenito e oásis', 1, '{"preset":"desert","size":"medium","seed":42,"scale":0.008,"octaves":6,"lacunarity":2,"gain":0.5,"waterLevel":0.08,"amplitude":0.4,"mountainThreshold":0.7,"mountainAmplitude":0.3,"cavesEnabled":false,"rivers":false,"volcano":false,"islandMode":false,"placeTrees":false,"treeDensity":0.001,"placeRocks":true,"rockDensity":0.003,"lighting":"desert"}'),
('skylands', 'Skylands', '🏔️', 'Ilhas flutuantes com cachoeiras e pontes naturais', 1, '{"preset":"skylands","size":"medium","seed":42,"scale":0.008,"octaves":6,"lacunarity":2,"gain":0.5,"waterLevel":0,"amplitude":0.8,"mountainThreshold":0.5,"mountainAmplitude":0.5,"cavesEnabled":false,"rivers":false,"volcano":false,"islandMode":true,"placeTrees":true,"treeDensity":0.02,"placeRocks":true,"rockDensity":0.005,"lighting":"tropical"}');

-- Assets padrão
INSERT INTO `asset_library` (`name`, `category`, `subcategory`, `biomes`, `lua_code`, `density_default`) VALUES
('Árvore Simples', 'tree', 'deciduous', 'FOREST,PLAINS,JUNGLE', 'local trunk=Instance.new("Part") trunk.Size=Vector3.new(2,10,2) trunk.Material=Enum.Material.Wood trunk.BrickColor=BrickColor.new("Reddish brown") trunk.Anchored=true\nlocal canopy=Instance.new("Part") canopy.Shape=Enum.PartType.Ball canopy.Size=Vector3.new(10,8,10) canopy.Material=Enum.Material.Grass canopy.BrickColor=BrickColor.new("Forest green") canopy.Anchored=true', 0.02),
('Pinheiro', 'tree', 'conifer', 'FOREST,TUNDRA', 'local trunk=Instance.new("Part") trunk.Size=Vector3.new(1.5,12,1.5) trunk.Material=Enum.Material.Wood trunk.BrickColor=BrickColor.new("Reddish brown") trunk.Anchored=true\nlocal canopy=Instance.new("Part") canopy.Size=Vector3.new(6,10,6) canopy.Material=Enum.Material.Grass canopy.BrickColor=BrickColor.new("Dark green") canopy.Anchored=true', 0.03),
('Palmeira', 'tree', 'palm', 'JUNGLE,DESERT', 'local trunk=Instance.new("Part") trunk.Size=Vector3.new(1.5,14,1.5) trunk.Material=Enum.Material.Wood trunk.BrickColor=BrickColor.new("Brown") trunk.Anchored=true\nlocal canopy=Instance.new("Part") canopy.Size=Vector3.new(8,3,8) canopy.Material=Enum.Material.Grass canopy.BrickColor=BrickColor.new("Bright green") canopy.Anchored=true', 0.01),
('Pedra Média', 'rock', 'boulder', 'PLAINS,FOREST,DESERT,TUNDRA,ARCTIC,VOLCANIC', NULL, 0.005),
('Pedra Grande', 'rock', 'large', 'FOREST,TUNDRA,VOLCANIC', NULL, 0.002),
('Arbusto', 'vegetation', 'bush', 'FOREST,PLAINS,JUNGLE', NULL, 0.03),
('Cogumelo', 'vegetation', 'mushroom', 'FOREST,SWAMP', NULL, 0.01),
('Cacto', 'vegetation', 'cactus', 'DESERT', NULL, 0.005),
('Cristal', 'prop', 'crystal', 'VOLCANIC,ARCTIC', NULL, 0.003),
('Fogueira', 'structure', 'campfire', 'FOREST,PLAINS', NULL, 0.001);

SET FOREIGN_KEY_CHECKS = 1;
