# 🗺️ Roblox Map Architect — Guia Completo

> **Projeto anônimo** — sem registro de usuário, sem login.
> Qualquer pessoa acessa e gera mapas. Identificação por session_id automático.

---

## 📐 Arquitetura

```
┌────────────────────────────────┐        ┌───────────────────────────────────┐
│  FRONTEND (HTML/CSS/JS puro)   │        │  BACKEND (PHP 8 + MySQL)          │
│  Netlify (grátis, uso comercial)│─fetch─→│  Hostinger (~R$11/mês)            │
│  seusite.netlify.app           │←JSON──│  seudominio.com/api/              │
│                                │        │                                   │
│  • Preview terreno em canvas   │        │  • 12 endpoints REST              │
│  • Configurador visual         │        │  • Gerador de scripts Lua         │
│  • Export Lua / JSON           │        │  • Histórico de mapas             │
│  • Zero build (HTML estático)  │        │  • Biblioteca de assets           │
│                                │        │                                   │
│  Deploy: git push → automático │        │  BD: techobco_roblox              │
│  Custo: GRÁTIS                 │        │  Custo: ~R$11/mês                 │
└────────────────────────────────┘        └───────────────────────────────────┘
```

**Por que Netlify?**
- Plano grátis **permite uso comercial** (Vercel proíbe)
- HTML estático = não gasta minutos de build
- Deploy automático via Git
- Domínio próprio grátis no plano free
- SSL automático

---

## 📁 Estrutura

```
roblox-map-gen/
├── api/                        ← HOSTINGER (public_html/api/)
│   ├── config.php              ← Credenciais BD + CORS
│   ├── index.php               ← API router
│   ├── Database.php            ← Classe PDO
│   ├── LuaGenerator.php        ← Gerador de Lua
│   └── .htaccess               ← Rewrite
├── sql/
│   └── schema.sql              ← 6 tabelas + dados iniciais
├── public/                     ← NETLIFY (root do deploy)
│   └── index.html              ← Frontend completo
└── README.md
```

---

## 🚀 Deploy Passo a Passo

### 1️⃣ Criar repositório Git no VS Code

```bash
cd roblox-map-gen
git init
git add .
git commit -m "v1 - roblox map generator"

# Criar repo no github.com → New Repository, depois:
git remote add origin https://github.com/SEU_USUARIO/roblox-map-architect.git
git branch -M main
git push -u origin main
```

---

### 2️⃣ Backend PHP na Hostinger

**Contratar:** Hostinger Premium (~R$11/mês) — inclui MySQL + SSL + domínio grátis 1º ano

**Subir arquivos via Gerenciador de Arquivos da Hostinger:**

```
public_html/
└── api/
    ├── config.php
    ├── index.php
    ├── Database.php
    ├── LuaGenerator.php
    └── .htaccess
```

**Criar banco MySQL no painel Hostinger:**
1. Painel → **Banco de Dados** → **MySQL**
2. Nome do banco: `techobco_roblox`
3. Usuário: `techobco_roblox`
4. Senha: `@Precisao2026`
5. Vincular usuário ao banco com **todos os privilégios**

**Instalar tabelas — acesse no navegador:**
```
https://seudominio.com/api/?action=install
```

**Verificar:**
```
https://seudominio.com/api/?action=stats
```

---

### 3️⃣ Frontend no Netlify (grátis)

1. Acesse **[netlify.com](https://www.netlify.com)**
2. Login com sua conta **GitHub**
3. Clique **"Add new site"** → **"Import an existing project"**
4. Selecione o repo `roblox-map-architect`
5. Configure:
   - **Base directory:** `public`
   - **Build command:** *(deixe vazio — é HTML puro)*
   - **Publish directory:** `public`
6. Clique **"Deploy site"**

Em 30 segundos seu site está no ar:
```
https://roblox-map-architect.netlify.app
```

> Netlify gera um nome aleatório tipo `silly-fox-abc123.netlify.app`.
> Vá em **Site configuration → Domain management → Edit site name**
> para trocar para `roblox-map-architect.netlify.app`.

---

### 4️⃣ Conectar Frontend → Backend

**No `public/index.html`**, altere a URL da API:

```javascript
// Troque esta linha:
const API = './api/';

// Para a URL da sua Hostinger:
const API = 'https://seudominio.com/api/';
```

**No `api/config.php`**, ajuste o CORS com seu link do Netlify:

```php
define('CORS_ORIGINS', [
    'https://roblox-map-architect.netlify.app',  // ← Seu link real
    'http://localhost',
    'http://127.0.0.1',
]);
```

**Suba as alterações:**

```bash
git add .
git commit -m "conectou frontend ao backend"
git push
# Netlify atualiza automaticamente em ~30 segundos
```

---

### 5️⃣ Domínio próprio (opcional)

**Comprar:**
- `.com.br` no Registro.br → ~R$40/ano
- `.com` no Namecheap → ~US$9/ano
- Hostinger dá domínio grátis no 1º ano

**DNS para Netlify:**
```
Tipo: A       Nome: @     Valor: 75.2.60.5
Tipo: CNAME   Nome: www   Valor: seusite.netlify.app
```

**DNS para API (subdomínio):**
```
Tipo: CNAME   Nome: api   Valor: seudominio.hostinger.com
```

**Resultado:**
```
meusite.com.br       →  Frontend no Netlify
api.meusite.com.br   →  PHP na Hostinger
```

SSL automático e grátis em ambos.

---

## 🔄 Workflow Diário

```bash
# No VS Code — edita, salva, e quando estiver pronto:
git add .
git commit -m "melhorei o preset vulcânico"
git push

# Pronto. Netlify detecta o push e faz deploy em ~30 segundos.
# Cada push gera um link de preview único antes de ir pro site principal.
# Só o push na branch 'main' atualiza o site real.
```

**Dica:** trabalhe o dia todo, faça vários commits locais, e
dê `push` só quando estiver satisfeito. Economiza deploys desnecessários.

**Extensões VS Code recomendadas:**
- `SFTP` — subir PHP pra Hostinger via FTP direto do VS Code
- `GitLens` — histórico visual de alterações
- `REST Client` — testar API sem sair do editor
- `Lua` — verificar syntax dos scripts gerados

---

## 🔌 API Endpoints

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `?action=presets` | Listar presets |
| `GET` | `?action=preset&id=X` | Obter preset |
| `POST` | `?action=preset_save` | Salvar preset custom |
| `DELETE` | `?action=preset_delete&id=X` | Deletar preset |
| `POST` | `?action=generate` | **Gerar mapa** (salva + Lua) |
| `GET` | `?action=maps&page=1&limit=20` | Listar mapas gerados |
| `GET` | `?action=map&id=X` | Obter mapa + script |
| `DELETE` | `?action=map_delete&id=X` | Deletar mapa |
| `GET` | `?action=materials` | Materiais Roblox (20) |
| `GET` | `?action=biomes` | Biomas (8) |
| `GET` | `?action=assets` | Assets (10) |
| `POST` | `?action=asset_save` | Novo asset |
| `GET` | `?action=stats` | Dashboard |
| `POST` | `?action=install` | Instalar BD |

---

## 🗄️ Banco de Dados

| Tabela | Dados iniciais | Descrição |
|--------|---------------|-----------|
| `presets` | 6 | Tropical, Medieval, Vulcânico, Ártico, Deserto, Skylands |
| `generated_maps` | 0 | Mapas gerados (Lua script + config) |
| `asset_library` | 10 | Árvores, pedras, cactos, cogumelos, etc. |
| `terrain_materials` | 20 | Materiais com RGB exato do Roblox |
| `biome_configs` | 8 | Plains, Forest, Jungle, Desert, Swamp, Tundra, Arctic, Volcanic |
| `generation_log` | 0 | Analytics de uso |

**Credenciais:**
```
Host:     localhost
Banco:    techobco_roblox
Usuário:  techobco_roblox
Senha:    @Precisao2026
```

---

## 💰 Custo Total

| Item | Custo | Serviço |
|------|-------|---------|
| Frontend | **Grátis** | Netlify |
| Backend + MySQL | **~R$11/mês** | Hostinger |
| Domínio .com.br | **~R$40/ano** (opcional) | Registro.br |
| SSL (HTTPS) | **Grátis** | Automático |
| **TOTAL mínimo** | **~R$11/mês** | |

---

## ✅ Checklist

- [ ] Criar repo no GitHub
- [ ] Subir PHP em `public_html/api/` na Hostinger
- [ ] Criar banco MySQL na Hostinger
- [ ] Acessar `?action=install` → criar tabelas
- [ ] Testar `?action=stats` → retorna JSON OK
- [ ] Conectar repo no Netlify (pasta `public`)
- [ ] Pegar link do Netlify (ex: `xxx.netlify.app`)
- [ ] Colocar link no `config.php` → `CORS_ORIGINS`
- [ ] Colocar URL da Hostinger no `index.html` → `const API`
- [ ] `git push` → testar geração completa
- [ ] (Opcional) Domínio próprio

---

## 🔧 Requisitos

- **Hostinger:** PHP 8.0+, MySQL 5.7+, PDO, mod_rewrite
- **Netlify:** Nada (HTML estático)
- **Dev local:** VS Code + Git + navegador
- **Opcional:** XAMPP para testar PHP localmente
