/* ============================================================
 * 插件市场 —— 插件源设置 + 在线插件列表 + 安装
 *
 * 两个易踩的点：
 *   1. POST /api/config/webui 是整文件覆盖，必须基于 rawWebui 深拷贝再改，
 *      否则会把 password_hash / session_secret 抹掉 → 账号与所有会话失效
 *   2. POST /api/plugins 是 POST 形式的 SSE，EventSource 用不了，
 *      走 Api.postStream（fetch + ReadableStream 手动分帧）
 * ============================================================ */
window.Pages = window.Pages || {};

// 内置 GitHub 镜像预设。都是在服务器上实测过 README 与 zip 两条链路都返回 200 的；
// 镜像随时可能失效，下拉支持直接粘贴自定义地址（filterable + allow-create）。
const GITHUB_MIRRORS = [
  { label: '不使用镜像（直连 GitHub）', value: '' },
  { label: 'ghfast.top', value: 'https://ghfast.top/' },
  { label: 'ghproxy.net', value: 'https://ghproxy.net/' },
  { label: 'gh-proxy.com', value: 'https://gh-proxy.com/' },
  { label: 'gh.llkk.cc', value: 'https://gh.llkk.cc/' },
];

window.Pages.Market = {
  name: 'MarketPage',
  template: `
  <div>
    <el-card shadow="never" class="form-section">
      <div class="form-section-title">插件源设置</div>
      <el-form label-position="top" @submit.prevent>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="6">
            <el-form-item label="使用 PyPI 镜像">
              <el-switch v-model="wc.use_pypi_mirror" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="9">
            <el-form-item label="PyPI 镜像地址">
              <el-input v-model="wc.pypi_mirror" placeholder="https://pypi.tuna.tsinghua.edu.cn/simple" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="9">
            <el-form-item label="GitHub 镜像（下载加速）">
              <!-- 可选预设，也能直接粘贴自定义地址（filterable + allow-create） -->
              <el-select v-model="wc.github_mirror" style="width:100%"
                         filterable allow-create default-first-option
                         placeholder="留空 = 直连 GitHub">
                <el-option v-for="m in mirrors" :key="m.value"
                           :label="m.label" :value="m.value" />
              </el-select>
              <div class="form-hint">
                连不上 GitHub 时才需要换镜像。镜像会代理你的下载流量，请选信得过的。
              </div>
            </el-form-item>
          </el-col>
        </el-row>
<el-row :gutter="16">
        <el-col :xs="24" :sm="8">
          <el-form-item label="GitHub PAT（可选，提高 API 限额）">
            <el-input v-model="wc.github_pat" show-password autocomplete="new-password"
                      name="lx-github-pat" placeholder="github_pat_..." />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="8">
          <el-form-item label="插件索引仓库">
            <el-input v-model="wc.plugins_index_repo" placeholder="Unisphere-Platform/LunarXU" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :sm="8" style="display:flex;align-items:flex-end">
          <el-form-item label=" ">
            <el-button type="primary" :loading="saving" @click="saveWebui">保存设置</el-button>
          </el-form-item>
        </el-col>
      </el-row>

      <el-divider content-position="left">多源插件配置</el-divider>

      <el-row :gutter="16">
        <el-col :xs="24" :sm="8">
          <el-form-item label="数据源优先级">
            <el-select v-model="wc.market_source_order" style="width:100%"
                      multiple allow-create collapse-tags collapse-tags-tooltip
                      placeholder="选择数据源顺序（从左到右优先）">
              <el-option label="Unisphere（推荐）" value="unisphere" />
              <el-option label="GitHub PluginIndex" value="github" />
            </el-select>
            <div class="form-hint">
              优先从 Unisphere 获取插件，失败时使用 GitHub PluginIndex
            </div>
          </el-form-item>
        </el-col>
      </el-row>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <div class="form-section-title">
        可安装插件
        <span style="flex:1"></span>
        <el-button size="small" text type="primary" :loading="loading" @click="load">刷新列表</el-button>
      </div>
      <div v-if="loading" v-loading="true" style="height:90px"></div>
      <el-table v-else :data="available" style="width:100%">
        <el-table-column prop="name" label="名称" min-width="160" />
        <el-table-column prop="description" label="描述" min-width="300" show-overflow-tooltip />
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button size="small" type="primary" plain @click="openInstall(row)">安装</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">
            点「刷新列表」从索引仓库拉取；后端要逐个取 README，首次可能需要数十秒。<br>
            已安装的插件不会出现在这里。
          </div>
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="installVisible" title="安装插件" width="560px" :close-on-click-modal="false" append-to-body>
      <el-form label-position="top">
        <el-form-item label="名称"><el-input :value="form.name" readonly /></el-form-item>
        <el-form-item label="下载地址"><el-input v-model="form.url" readonly /></el-form-item>
        <el-form-item label="安装路径"><el-input v-model="form.path" readonly /></el-form-item>
      </el-form>
      <el-progress v-if="installing" :percentage="percent" :indeterminate="percent < 100"
                   style="margin-bottom:10px" />
      <pre v-if="installing || log" ref="logEl" class="install-log">{{ log }}{{ tail }}</pre>
      <template #footer>
        <el-button :disabled="installing" @click="installVisible = false">关闭</el-button>
        <el-button type="primary" :loading="installing" @click="start">开始安装</el-button>
      </template>
    </el-dialog>
  </div>
  `,

  data() {
    return {
      mirrors: GITHUB_MIRRORS,
      rawWebui: null,
      wc: {
        use_pypi_mirror: false,
        pypi_mirror: '',
        github_mirror: '',
        github_pat: '',
        plugins_index_repo: 'Unisphere-Platform/LunarXU',
        market_source_order: ['unisphere', 'github']
      },
      saving: false,

      available: [],
      loading: false,

      installVisible: false,
      installing: false,
      percent: 0,
      log: '',
      tail: '',        // 「下载中 x/y」这类高频消息单独放，原地覆盖不刷屏
      form: { name: '', url: '', path: '' },
    };
  },

  mounted() {
    // 浏览器会把保存的站点账号密码回填进这张表单（镜像框被填成用户名、
    // PAT 框被填成密码），一保存就把密码写进配置里。逐个关掉自动填充。
    this.$nextTick(() => {
      const root = this.$el && this.$el.querySelector ? this.$el : null;
      if (!root) return;
      root.querySelectorAll('input').forEach(el => {
        el.setAttribute('autocomplete', 'off');
        el.setAttribute('data-lpignore', 'true');   // LastPass
        el.setAttribute('data-1p-ignore', 'true');  // 1Password
      });
    });
    this.loadWebui();
    // 市场列表后端要逐个拉 README，最坏几十秒，不自动加载，由用户点刷新
  },

  methods: {
    async loadWebui() {
      try {
        const raw = await Api.get('/api/config/webui');
        this.rawWebui = raw;
        this.wc = {
          use_pypi_mirror: raw.use_pypi_mirror === true,
          pypi_mirror: raw.pypi_mirror || '',
          github_mirror: raw.github_mirror || '',
          github_pat: raw.github_pat || '',
          plugins_index_repo: raw.plugins_index_repo || 'Unisphere-Platform/LunarXU',
          market_source_order: raw.market_source_order || ['unisphere', 'github']
        };
      } catch (e) {
        this.$message.error('加载插件源设置失败：' + e.message);
      }
    },

    async saveWebui() {
      if (!this.rawWebui) {
        this.$message.error('配置尚未加载完成，无法保存');
        return;
      }
      this.saving = true;
      try {
        // 深拷贝原文件再覆盖，保住 password_hash / session_secret
        const out = Object.assign(JSON.parse(JSON.stringify(this.rawWebui)), this.wc);
        await Api.post('/api/config/webui', out);
        this.rawWebui = out;
        this.$message.success('插件源设置已保存');
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.saving = false;
      }
    },

    async load() {
      this.loading = true;
      try {
        const data = await Api.get('/api/available_plugins');
        this.available = Array.isArray(data) ? data : [];
        if (!this.available.length) this.$message.info('暂无可安装的插件，请检查插件源配置');
      } catch (e) {
        this.available = [];
        this.$message.error('加载插件列表失败：' + e.message);
      } finally {
        this.loading = false;
      }
    },

    openInstall(row) {
      this.form = { id: row.id, name: row.name, url: row.url, path: row.path };
      this.log = '';
      this.tail = '';
      this.percent = 0;
      this.installing = false;
      this.installVisible = true;
    },

    append(line) {
      // 后端下载阶段每 8KB 推一条「下载中 x/y」，全追加会刷出上千行，
      // 这类进度消息只保留最后一条
      if (/^下载中[:：]/.test(line)) {
        this.tail = line + '\n';
      } else {
        this.log += line + '\n';
        this.tail = '';
      }
      this.$nextTick(() => {
        const el = this.$refs.logEl;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    async start() {
      if (this.installing) return;
      this.installing = true;
      this.percent = 0;
      this.log = '';
      this.tail = '';

      let finished = false;
      try {
        await Api.postStream('/api/plugins', {
          name: this.form.id,
          url: this.form.url,
          path: this.form.path,
          use_pypi_mirror: this.wc.use_pypi_mirror,
          pypi_mirror: this.wc.pypi_mirror,
          // 后端用它匹配解压出的 <repo>-main 目录
          plugins_index_repo_name_only: (this.wc.plugins_index_repo || '').split('/').pop() || 'Unisphere',
        }, (line) => {
          if (line === 'INSTALL_SUCCESS') {
            finished = true;
            this.percent = 100;
            this.$message.success('安装成功，可到「插件管理」查看');
            this.load();
            return;
          }
          if (line === 'INSTALL_FAILED') {
            finished = true;
            this.$message.error('安装失败');
            return;
          }
          this.append(line);
          // 后端不报真实百分比，这里是节奏指示而非精确进度
          this.percent = Math.min(95, this.percent + 4);
        });
        if (!finished) this.$message.warning('连接已结束，但未收到安装结果，请查看日志');
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.installing = false;
      }
    },
  },
};
