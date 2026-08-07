/* 插件管理 */
window.Pages = window.Pages || {};
window.Pages.Plugins = {
    name: 'PluginsPage',
    template: `
    <div>
      <h2 class="page-title">插件管理</h2>
      <p class="page-sub">插件源设置、已安装插件与在线插件市场</p>

      <el-card shadow="never" class="form-section">
        <div class="form-section-title">插件源设置</div>
        <el-form label-position="top">
          <el-row :gutter="16">
            <el-col :span="6">
              <el-form-item label="使用 PyPI 镜像">
                <el-switch v-model="wc.use_pypi_mirror" />
              </el-form-item>
            </el-col>
            <el-col :span="9">
              <el-form-item label="PyPI 镜像地址">
                <el-input v-model="wc.pypi_mirror" />
              </el-form-item>
            </el-col>
            <el-col :span="9">
              <el-form-item label="GitHub 镜像（下载加速）">
                <el-input v-model="wc.github_mirror" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :span="8">
              <el-form-item label="GitHub PAT（可选，提高 API 限额）">
                <el-input v-model="wc.github_pat" show-password placeholder="github_pat_..." />
              </el-form-item>
            </el-col>
            <el-col :span="10">
              <el-form-item label="插件索引仓库">
                <el-input v-model="wc.plugins_index_repo" placeholder="MacroSTAR-Org/Unisphere" />
              </el-form-item>
            </el-col>
            <el-col :span="6" style="display:flex;align-items:flex-end;">
              <el-button type="primary" :loading="saving" @click="saveWebui">保存设置</el-button>
            </el-col>
          </el-row>
        </el-form>
      </el-card>

      <el-card shadow="never" class="form-section">
        <div class="form-section-title">已安装插件</div>
        <el-table :data="installed" style="width: 100%">
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column prop="type" label="类型" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.type === 'directory' ? 'primary' : 'info'">
                {{ row.type === 'directory' ? '目录' : '文件' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" @change="v => togglePlugin(row, v)" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="200">
            <template #default="{ row }">
              <el-button size="small" text type="primary" v-if="row.has_help" @click="showHelp(row)">帮助</el-button>
              <el-button size="small" text type="danger" @click="removePlugin(row)">卸载</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never">
        <div class="form-section-title">
          可用插件
          <el-button size="small" text type="primary" :loading="loadingAvailable" @click="loadAvailable" style="margin-left:8px;">
            刷新列表
          </el-button>
        </div>
        <div v-if="loadingAvailable" v-loading="true" style="height: 80px;"></div>
        <el-table v-else :data="available" style="width: 100%">
          <el-table-column prop="name" label="名称" min-width="150" />
          <el-table-column prop="description" label="描述" min-width="280" show-overflow-tooltip />
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button size="small" type="primary" plain @click="openInstall(row)">安装</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 安装弹窗（SSE 进度） -->
      <el-dialog v-model="installVisible" title="安装插件" width="560px" :close-on-click-modal="false">
        <el-form label-position="top">
          <el-form-item label="名称">
            <el-input v-model="installForm.name" readonly />
          </el-form-item>
          <el-form-item label="下载地址">
            <el-input v-model="installForm.url" readonly />
          </el-form-item>
          <el-form-item label="安装路径">
            <el-input v-model="installForm.path" readonly />
          </el-form-item>
        </el-form>
        <div v-if="installing">
          <el-progress :percentage="installPercent" :indeterminate="installPercent < 100" />
          <pre style="background:#101114;color:#d6d8dc;border-radius:12px;padding:12px;height:140px;overflow-y:auto;font-size:12px;margin-top:12px;">{{ installLog }}</pre>
        </div>
        <template #footer>
          <el-button @click="installVisible = false" :disabled="installing">关闭</el-button>
          <el-button type="primary" :loading="installing" @click="startInstall">开始安装</el-button>
        </template>
      </el-dialog>

      <!-- 帮助弹窗 -->
      <el-dialog v-model="helpVisible" title="插件帮助" width="620px">
        <pre style="white-space:pre-wrap;word-break:break-all;line-height:1.7;">{{ helpContent }}</pre>
      </el-dialog>
    </div>
    `,
    data() {
        return {
            rawWc: null,
            wc: { use_pypi_mirror: false, pypi_mirror: '', github_mirror: '', github_pat: '', plugins_index_repo: '' },
            saving: false,
            installed: [],
            available: [],
            loadingAvailable: false,
            installVisible: false,
            installing: false,
            installPercent: 0,
            installLog: '',
            installForm: { name: '', url: '', path: '' },
            helpVisible: false,
            helpContent: '',
        };
    },
    mounted() { this.loadWebui(); this.loadInstalled(); },
    methods: {
        async loadWebui() {
            try {
                const resp = await fetch('/api/config/webui', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                this.rawWc = await resp.json();
                const w = this.rawWc;
                this.wc = {
                    use_pypi_mirror: !!w.use_pypi_mirror,
                    pypi_mirror: w.pypi_mirror || '',
                    github_mirror: w.github_mirror || '',
                    github_pat: w.github_pat || '',
                    plugins_index_repo: w.plugins_index_repo || '',
                };
            } catch (e) { this.$message.error('加载插件源设置失败'); }
        },
        async saveWebui() {
            if (!this.rawWc) return;
            this.saving = true;
            try {
                const out = JSON.parse(JSON.stringify(this.rawWc));
                Object.assign(out, this.wc);
                const resp = await fetch('/api/config/webui', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(out),
                });
                const data = await resp.json();
                if (resp.ok) this.$message.success(data.message || '保存成功');
                else this.$message.error(data.error || '保存失败');
            } catch (e) {
                this.$message.error('保存失败: ' + e.message);
            } finally {
                this.saving = false;
            }
        },
        async loadInstalled() {
            try {
                const resp = await fetch('/api/plugins', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                this.installed = await resp.json();
            } catch (e) { this.$message.error('加载插件列表失败'); }
        },
        async togglePlugin(row, val) {
            try {
                const resp = await fetch('/api/plugins/' + encodeURIComponent(row.name), { method: 'PUT' });
                const data = await resp.json();
                if (resp.ok) {
                    row.enabled = val;
                    this.$message.success(val ? '已启用: ' + row.name : '已禁用: ' + row.name);
                } else {
                    this.$message.error(data.error || '操作失败');
                }
            } catch (e) {
                this.$message.error('操作失败: ' + e.message);
            }
        },
        removePlugin(row) {
            this.$confirm('确定卸载插件「' + row.name + '」？', '卸载确认', { type: 'warning' })
                .then(async () => {
                    const resp = await fetch('/api/plugins/' + encodeURIComponent(row.name), { method: 'DELETE' });
                    const data = await resp.json();
                    if (resp.ok) {
                        this.$message.success(data.message || '已卸载');
                        this.loadInstalled();
                    } else {
                        this.$message.error(data.error || '卸载失败');
                    }
                })
                .catch(() => {});
        },
        async showHelp(row) {
            try {
                const resp = await fetch('/api/plugins/' + encodeURIComponent(row.name));
                const data = await resp.json();
                this.helpContent = data.help || '（无帮助文档）';
                this.helpVisible = true;
            } catch (e) { this.$message.error('获取帮助失败'); }
        },
        async loadAvailable() {
            this.loadingAvailable = true;
            try {
                const resp = await fetch('/api/available_plugins', { cache: 'no-store' });
                if (resp.status === 401) { location.href = '/login'; return; }
                if (!resp.ok) {
                    const data = await resp.json();
                    this.$message.error(data.error || '获取可用插件失败');
                    this.available = [];
                    return;
                }
                this.available = await resp.json();
            } catch (e) {
                this.available = [];
                this.$message.error('获取可用插件失败');
            } finally {
                this.loadingAvailable = false;
            }
        },
        openInstall(row) {
            this.installForm = { name: row.name, url: row.url, path: row.path };
            this.installPercent = 0;
            this.installLog = '';
            this.installing = false;
            this.installVisible = true;
        },
        async startInstall() {
            this.installing = true;
            this.installPercent = 0;
            this.installLog = '';
            const body = {
                name: this.installForm.name,
                url: this.installForm.url,
                path: this.installForm.path,
                use_pypi_mirror: this.wc.use_pypi_mirror,
                pypi_mirror: this.wc.pypi_mirror,
                plugins_index_repo_name_only: this.rawWc ? this.rawWc.plugins_index_repo : '',
            };
            try {
                const resp = await fetch('/api/plugins', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buf = '';
                let done = false;
                while (!done) {
                    const { value, done: d } = await reader.read();
                    done = d;
                    buf += decoder.decode(value || new Uint8Array(), { stream: !done });
                    const frames = buf.split('\n\n');
                    buf = frames.pop() || '';
                    for (const frame of frames) {
                        const line = frame.replace(/^data:\s*/, '').trim();
                        if (!line) continue;
                        if (line === 'INSTALL_SUCCESS') {
                            this.installPercent = 100;
                            this.$message.success('安装成功');
                            this.installing = false;
                            this.loadInstalled();
                            return;
                        }
                        if (line === 'INSTALL_FAILED') {
                            this.$message.error('安装失败');
                            this.installing = false;
                            return;
                        }
                        this.installLog += line + '\n';
                        const el = this.$refs.installLogEl;
                        if (el) el.scrollTop = el.scrollHeight;
                        this.installPercent = Math.min(95, this.installPercent + 8);
                    }
                }
            } catch (e) {
                this.$message.error('安装失败: ' + e.message);
                this.installing = false;
            }
        },
    },
};
