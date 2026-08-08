/* ============================================================
 * 设置（四合一）
 *   协议端  → appsettings.json
 *   Bot 端  → config.json  ┐ 共用一份 raw 快照，
 *   人格    → config.json  ┘ 避免两页分别持快照互相覆盖
 *   用户    → admin114.json
 *
 * 后端 POST /api/config/<type> 是**整文件覆盖**，所以每次保存都必须
 * 基于加载时的 raw 深拷贝再改字段，未在 UI 暴露的键才不会丢。
 * ============================================================ */
window.Pages = window.Pages || {};

const TABS = ['protocol', 'bot', 'users'];

window.Pages.Settings = {
  name: 'SettingsPage',
  props: { subRoute: { type: String, default: '' } },

  template: `
  <el-tabs v-model="tab" @tab-change="onTabChange">

    <!-- ========== 协议端 ========== -->
    <el-tab-pane label="协议端" name="protocol">
      <el-card shadow="never">
        <div class="form-section-title">Milky 协议端（LLBot / Lagrange）</div>
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="签名服务器 URL（SignServerUrl）">
            <el-input v-model="proto.SignServerUrl" placeholder="https://sign.lagrangecore.org/api/sign/39038" />
            <div class="form-hint">签名服务地址，需与协议端版本匹配</div>
          </el-form-item>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8">
              <el-form-item label="监听 Host">
                <el-input v-model="impl.Host" placeholder="127.0.0.1" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="端口">
                <el-input v-model.number="impl.Port" placeholder="3010" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="AccessToken">
                <el-input v-model="impl.AccessToken" show-password placeholder="协议端鉴权 token" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="机器人 QQ（Uin）">
            <el-input v-model.number="account.Uin" placeholder="QQ 号" />
          </el-form-item>
          <div class="form-actions">
            <el-button type="primary" :loading="saving.protocol" @click="saveProtocol">保存配置</el-button>
          </div>
        </el-form>
      </el-card>
    </el-tab-pane>

    <!-- ========== Bot 端 ========== -->
    <el-tab-pane label="Bot 端" name="bot">
      <el-card shadow="never">
        <div class="form-section-title">框架基础</div>
        <el-form label-position="top" @submit.prevent>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8">
              <el-form-item label="机器人名称"><el-input v-model="bot.bot_name" /></el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="英文名称"><el-input v-model="bot.bot_name_en" /></el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="命令触发词">
                <el-input v-model="bot.trigger_keyword" placeholder="$" />
                <div class="form-hint">如 $，消息以 $ 开头视为命令</div>
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8">
              <el-form-item label="插件热重载">
                <el-select v-model="bot.auto_reload_plugins" style="width:100%">
                  <el-option label="开启" :value="true" />
                  <el-option label="关闭" :value="false" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="日志级别">
                <el-select v-model="bot.log_level" style="width:100%">
                  <el-option v-for="lv in logLevels" :key="lv" :label="lv" :value="lv" />
                </el-select>
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
      </el-card>

    </el-tab-pane>

    <!-- ========== 用户 ========== -->
    <el-tab-pane label="用户" name="users">
      <el-card shadow="never">
        <div class="form-section-title">管理员名单</div>
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="超级管理员（super_users）">
            <el-input v-model="users.super_users" type="textarea" :rows="3" placeholder="123456, 789012" />
            <div class="form-hint">拥有全部权限，包括重启机器人、删除语录等</div>
          </el-form-item>
          <el-form-item label="管理员（manager_users）">
            <el-input v-model="users.manager_users" type="textarea" :rows="3" placeholder="123456, 789012" />
            <div class="form-hint">可执行禁言、踢人、删除语录等群管理操作</div>
          </el-form-item>
          <div class="form-actions">
            <el-button type="primary" :loading="saving.users" @click="saveUsers">保存配置</el-button>
          </div>
        </el-form>
      </el-card>
    </el-tab-pane>

  </el-tabs>
  `,

  data() {
    return {
      tab: 'protocol',
      logLevels: ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
      saving: { protocol: false, bot: false, users: false },

      // 加载时的原始文件快照，保存时深拷贝它再改，未暴露的字段不会丢
      rawAppsettings: null,
      rawConfig: null,
      rawAdmin: null,

      proto: { SignServerUrl: '' },
      impl: { Host: '127.0.0.1', Port: 3010, AccessToken: '' },
      account: { Uin: 0 },

      bot: { bot_name: '', bot_name_en: '', trigger_keyword: '$', auto_reload_plugins: true, log_level: 'INFO' },
      users: { super_users: '', manager_users: '' },
    };
  },

  created() {
    if (TABS.includes(this.subRoute)) this.tab = this.subRoute;
  },

  watch: {
    // 组件按 page 做 key，子路由变化不会重建组件，必须监听同步过来，
    // 否则浏览器前进/后退、或从 #/settings/bot 跳 #/settings/users 时标签不动
    subRoute(v) {
      if (TABS.includes(v) && v !== this.tab) this.tab = v;
    },
  },

  mounted() {
    this.loadAppsettings();
    this.loadConfig();
    this.loadAdmin();
  },

  methods: {
    /** 切换子标签时同步到 hash，刷新页面能回到同一标签 */
    onTabChange(name) {
      location.hash = '#/settings/' + name;
    },

    // ---------- 协议端 ----------
    async loadAppsettings() {
      try {
        const raw = await Api.get('/api/config/appsettings');
        this.rawAppsettings = raw;
        this.proto.SignServerUrl = raw.SignServerUrl || '';
        const impls = raw.Implementations || [];
        const i = impls[0] || {};
        this.impl = {
          Host: i.Host || '127.0.0.1',
          Port: i.Port != null ? i.Port : 3010,
          AccessToken: i.AccessToken || '',
        };
        this.account = Object.assign({ Uin: 0 }, raw.Account || {});
      } catch (e) {
        this.$message.error('加载协议端配置失败：' + e.message);
      }
    },

    async saveProtocol() {
      if (!this.rawAppsettings) {          // 加载失败时不能保存，否则会用空对象覆盖文件
        this.$message.error('配置尚未加载完成，无法保存');
        return;
      }
      this.saving.protocol = true;
      try {
        const out = JSON.parse(JSON.stringify(this.rawAppsettings));
        out.SignServerUrl = this.proto.SignServerUrl;
        if (!Array.isArray(out.Implementations) || !out.Implementations.length) out.Implementations = [{}];
        out.Implementations[0] = Object.assign({}, out.Implementations[0], {
          Host: this.impl.Host,
          Port: Number(this.impl.Port) || 0,
          AccessToken: this.impl.AccessToken,
        });
        out.Account = Object.assign({}, out.Account, this.account);
        await Api.post('/api/config/appsettings', out);
        this.rawAppsettings = out;
        this.$message.success('协议端配置已保存');
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.saving.protocol = false;
      }
    },

    // ---------- config.json（Bot 端 + 人格共用） ----------
    async loadConfig() {
      try {
        const raw = await Api.get('/api/config/config');
        this.rawConfig = raw;

        this.bot = {
          bot_name: raw.bot_name || '',
          bot_name_en: raw.bot_name_en || '',
          trigger_keyword: raw.trigger_keyword || '$',
          auto_reload_plugins: raw.auto_reload_plugins !== false,
          log_level: raw.log_level || 'INFO',
        };

      } catch (e) {
        this.$message.error('加载 Bot 配置失败：' + e.message);
      }
    },

    /** Bot 端与人格都写 config.json，统一走这里，写完刷新本地快照 */
    async saveConfig(mutate, flag, okMsg) {
      if (!this.rawConfig) {
        this.$message.error('配置尚未加载完成，无法保存');
        return;
      }
      this.saving[flag] = true;
      try {
        const out = JSON.parse(JSON.stringify(this.rawConfig));
        mutate(out);
        await Api.post('/api/config/config', out);
        this.rawConfig = out;
        this.$message.success(okMsg);
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.saving[flag] = false;
      }
    },

    saveBot() {
      return this.saveConfig(out => {
        Object.assign(out, this.bot);
      }, 'bot', 'Bot 配置已保存');
    },

    // ---------- 用户 ----------
    async loadAdmin() {
      try {
        const raw = await Api.get('/api/config/admin');
        this.rawAdmin = raw;
        this.users = {
          super_users: (raw.super_users || []).join(', '),
          manager_users: (raw.manager_users || []).join(', '),
        };
      } catch (e) {
        this.$message.error('加载用户配置失败：' + e.message);
      }
    },

    /** 中英文逗号、空格、换行都能作分隔符 */
    parseQQList(s) {
      return String(s || '')
        .split(/[,，\s]+/)
        .map(x => x.trim())
        .filter(Boolean)
        .map(Number)
        .filter(n => !isNaN(n));
    },

    async saveUsers() {
      if (!this.rawAdmin) {
        this.$message.error('配置尚未加载完成，无法保存');
        return;
      }
      this.saving.users = true;
      try {
        const out = JSON.parse(JSON.stringify(this.rawAdmin));
        out.super_users = this.parseQQList(this.users.super_users);
        out.manager_users = this.parseQQList(this.users.manager_users);
        await Api.post('/api/config/admin', out);
        this.rawAdmin = out;
        this.$message.success('用户配置已保存');
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.saving.users = false;
      }
    },
  },
};
