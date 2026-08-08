/* ============================================================
 * 插件管理 —— 只管已经装上的插件
 *   启用/禁用（后端靠 d_ 前缀重命名实现，是无参 toggle）
 *   按插件自带的 plugin.json 动态渲染配置表单
 *   查看帮助文档 / 卸载
 * 安装新插件在「插件市场」页（market.js）
 *
 * 配置表单完全由后端返回的 schema 驱动：新增插件不需要改这里任何代码。
 * ============================================================ */
window.Pages = window.Pages || {};

window.Pages.Plugins = {
  name: 'PluginsPage',
  // 注意：弹窗必须放在 el-card 外面。Element Plus 的 el-dialog 默认不 teleport
  // 到 body，卡片上的 overflow:hidden 会把它裁掉半截。
  template: `
  <div>
  <el-card shadow="never">
    <div class="form-section-title">
      已安装插件
      <span style="flex:1"></span>
      <el-button size="small" text type="primary" :loading="loading" @click="load">刷新</el-button>
    </div>
    <el-table :data="installed" style="width:100%" v-loading="loading">
      <el-table-column label="插件" min-width="220">
        <template #default="{ row }">
          <div style="font-weight:600">{{ row.display_name || row.name }}</div>
          <div class="plugin-sub">
            <span class="mono">{{ row.name }}</span>
            <template v-if="row.version"> · v{{ row.version }}</template>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="260" show-overflow-tooltip />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          <el-tag size="small">{{ row.type === 'directory' ? '目录' : '文件' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="() => toggle(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230">
        <template #default="{ row }">
          <el-button v-if="row.has_config" text type="primary" size="small" @click="openConfig(row)">配置</el-button>
          <el-button v-if="row.has_help" text type="primary" size="small" @click="showHelp(row)">帮助</el-button>
          <el-button text type="danger" size="small" @click="remove(row)">卸载</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-hint">还没有安装任何插件，去「插件市场」看看</div>
      </template>
    </el-table>
  </el-card>

    <!-- 配置弹窗：按 schema 逐项渲染，控件类型由后端声明 -->
    <el-dialog v-model="configVisible" :title="configTitle" width="620px" top="6vh" append-to-body>
      <div v-loading="configLoading">
        <div v-if="manifest && manifest.description" class="form-hint" style="margin-bottom:16px">
          {{ manifest.description }}
        </div>
        <el-form label-position="top" @submit.prevent>
          <el-form-item v-for="item in schema" :key="item.key" :label="item.label || item.key">
            <el-switch v-if="item.type === 'bool'" v-model="form[item.key]" />

            <el-input v-else-if="item.type === 'secret'" v-model="form[item.key]"
                      show-password :placeholder="item.placeholder || ''" />

            <el-input v-else-if="item.type === 'text'" v-model="form[item.key]"
                      type="textarea" :rows="item.rows || 3" :placeholder="item.placeholder || ''" />

            <el-input-number v-else-if="item.type === 'int' || item.type === 'number'"
                             v-model="form[item.key]" style="width:220px"
                             :min="item.min" :max="item.max"
                             :step="item.step || (item.type === 'int' ? 1 : 0.1)"
                             :precision="item.type === 'int' ? 0 : undefined" />

            <el-select v-else-if="item.type === 'select'" v-model="form[item.key]" style="width:260px">
              <el-option v-for="op in (item.options || [])" :key="op.value"
                         :label="op.label" :value="op.value" />
            </el-select>

            <!-- 列表类：用多行文本编辑，一行一项，提交时后端按类型转换 -->
            <el-input v-else-if="item.type === 'string_list' || item.type === 'int_list'"
                      v-model="form[item.key]" type="textarea" :rows="item.rows || 3"
                      :placeholder="item.placeholder || '每行一个'" />

            <el-input v-else v-model="form[item.key]" :placeholder="item.placeholder || ''" />

            <div class="form-hint" v-if="item.hint">{{ item.hint }}</div>
          </el-form-item>
        </el-form>
        <div v-if="!schema.length" class="empty-hint">该插件没有声明可配置项</div>
      </div>
      <template #footer>
        <el-button @click="configVisible = false">取消</el-button>
        <el-button type="primary" :loading="configSaving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="helpVisible" :title="helpTitle" width="620px" append-to-body>
      <pre style="white-space:pre-wrap;word-break:break-all;line-height:1.7;margin:0">{{ helpContent || '（无帮助文档）' }}</pre>
    </el-dialog>
  </div>
  `,

  data() {
    return {
      installed: [], loading: false,
      helpVisible: false, helpTitle: '插件帮助', helpContent: '',
      configVisible: false, configLoading: false, configSaving: false,
      configTitle: '插件配置', configName: '',
      manifest: null, schema: [], form: {},
    };
  },

  mounted() { this.load(); },

  methods: {
    async load() {
      this.loading = true;
      try {
        const data = await Api.get('/api/plugins');
        this.installed = Array.isArray(data) ? data : [];
      } catch (e) {
        this.$message.error('加载插件列表失败：' + e.message);
      } finally {
        this.loading = false;
      }
    },

    async toggle(row) {
      try {
        // 后端是无参 toggle，不能指定目标状态，改完重新拉列表以真实状态为准
        const data = await Api.put('/api/plugins/' + encodeURIComponent(row.name));
        this.$message.success((data && data.message) || '已切换');
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.load();
      }
    },

    async remove(row) {
      try {
        await this.$confirm('确定卸载插件「' + row.name + '」？', '卸载确认', { type: 'warning' });
      } catch (e) {
        return;
      }
      try {
        await Api.del('/api/plugins/' + encodeURIComponent(row.name));
        this.$message.success('已卸载');
        this.load();
      } catch (e) {
        this.$message.error(e.message);
      }
    },

    async showHelp(row) {
      try {
        const data = await Api.get('/api/plugins/' + encodeURIComponent(row.name));
        this.helpTitle = (row.display_name || row.name) + ' · 帮助';
        this.helpContent = (data && data.help) || '';
        this.helpVisible = true;
      } catch (e) {
        this.$message.error(e.message);
      }
    },

    // ---------- 插件配置 ----------
    async openConfig(row) {
      this.configName = row.name;
      this.configTitle = (row.display_name || row.name) + ' · 配置';
      this.manifest = null;
      this.schema = [];
      this.form = {};
      this.configVisible = true;
      this.configLoading = true;
      try {
        const name = encodeURIComponent(row.name);
        const [manifest, cfg] = await Promise.all([
          Api.get('/api/plugins/' + name + '/manifest'),
          Api.get('/api/plugins/' + name + '/config'),
        ]);
        this.manifest = manifest;
        this.schema = manifest.config || [];
        const values = (cfg && cfg.values) || {};
        // 列表类在表单里用多行文本编辑，进来时拍成一行一项
        const form = {};
        for (const item of this.schema) {
          const v = values[item.key];
          form[item.key] = (item.type === 'string_list' || item.type === 'int_list')
            ? (Array.isArray(v) ? v.join('\n') : (v == null ? '' : String(v)))
            : v;
        }
        this.form = form;
      } catch (e) {
        this.$message.error('加载配置失败：' + e.message);
        this.configVisible = false;
      } finally {
        this.configLoading = false;
      }
    },

    async saveConfig() {
      this.configSaving = true;
      try {
        // 列表类回传数组，其余原样；类型校验在后端按 schema 做
        const payload = {};
        for (const item of this.schema) {
          const v = this.form[item.key];
          payload[item.key] = (item.type === 'string_list' || item.type === 'int_list')
            ? String(v == null ? '' : v).split('\n').map(s => s.trim()).filter(Boolean)
            : v;
        }
        const data = await Api.post(
          '/api/plugins/' + encodeURIComponent(this.configName) + '/config', payload);
        this.$message.success((data && data.message) || '已保存');
        this.configVisible = false;
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.configSaving = false;
      }
    },
  },
};
