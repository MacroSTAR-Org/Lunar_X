/* ============================================================
 * 设置页（WebUI 自身：个性化 + 账户与安全）
 *
 * 个性化 → localStorage（lunarx_prefs），纯前端，改完即生效
 * 账户与安全 → webui.json（/api/webui_security + /api/change_password）
 * 登录日志 → GET /api/login_logs（logs/webui_login.log 倒序）
 * ============================================================ */
window.Pages = window.Pages || {};

const PREFS_KEY = 'lunarx_prefs';
const THEME_KEY = 'lunarx_theme';

window.Pages.Settings = {
  name: 'SettingsPage',
  props: { subRoute: { type: String, default: '' } },

  template: `
  <div class="settings-grid">

    <!-- ========== 个性化 ========== -->
    <el-card shadow="never">
      <div class="form-section-title">个性化</div>
      <el-form label-position="top" @submit.prevent>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="8">
            <el-form-item label="主题">
              <el-select v-model="prefs.theme" style="width:100%" @change="onThemeChange">
                <el-option label="深色" value="dark" />
                <el-option label="浅色" value="light" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-form-item label="默认首页">
              <el-select v-model="prefs.default_page" style="width:100%">
                <el-option v-for="p in pages" :key="p.id" :label="p.label" :value="p.id" />
              </el-select>
              <div class="form-hint">登录后进入的页面</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-form-item label="看板刷新间隔（秒）">
              <el-input-number v-model="prefs.dashboard_refresh" :min="3" :max="300" :step="1" style="width:100%" />
              <div class="form-hint">数据看板自动轮询间隔</div>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :xs="24" :sm="8">
            <el-form-item label="背景动效">
              <el-switch v-model="prefs.bg_effects" active-text="开" inactive-text="关" />
              <div class="form-hint">背景点阵漂移动画；关闭可降低 CPU 占用</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="8">
            <el-form-item label="界面密度">
              <el-select v-model="prefs.density" style="width:100%">
                <el-option label="标准" value="standard" />
                <el-option label="紧凑" value="compact" />
              </el-select>
              <div class="form-hint">紧凑模式压缩卡片与间距，单屏可看更多</div>
            </el-form-item>
          </el-col>
        </el-row>
        <div class="form-actions">
          <el-button type="primary" @click="savePrefs">保存个性化</el-button>
        </div>
      </el-form>
    </el-card>

    <!-- ========== 账户与安全 ========== -->
    <el-card shadow="never">
      <div class="form-section-title">账户与安全</div>

      <!-- 凭据 -->
      <div class="form-section">
        <div class="form-sub-title">登录凭据</div>
        <el-form label-position="top" @submit.prevent>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8">
              <el-form-item label="登录用户名">
                <el-input v-model="cred.username" maxlength="32" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="新密码">
                <el-input v-model="cred.new1" type="password" show-password autocomplete="new-password" />
                <div class="form-hint">不修改请留空；至少 4 位</div>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="确认新密码">
                <el-input v-model="cred.new2" type="password" show-password autocomplete="new-password" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="当前密码">
            <el-input v-model="cred.old" type="password" show-password autocomplete="current-password" style="max-width:320px" />
            <div class="form-hint">修改用户名或密码时必须填写当前密码</div>
          </el-form-item>
          <div class="form-actions">
            <el-button :loading="saving.cred" @click="saveCred">保存凭据</el-button>
          </div>
        </el-form>
      </div>

      <!-- 会话与安全策略 -->
      <div class="form-section">
        <div class="form-sub-title">会话与安全策略</div>
        <el-form label-position="top" @submit.prevent>
          <el-row :gutter="16">
            <el-col :xs="24" :sm="8">
              <el-form-item label="会话有效期（分钟）">
                <el-input-number v-model="sec.session_ttl_minutes" :min="0" :max="10080" :step="30" style="width:100%" />
                <div class="form-hint">无操作多久后自动退出；0 = 永不过期</div>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="失败锁定阈值">
                <el-input-number v-model="sec.login_fail_max" :min="1" :max="20" style="width:100%" />
                <div class="form-hint">连续失败多少次后临时锁定</div>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-form-item label="锁定时间（分钟）">
                <el-input-number v-model="sec.login_lock_minutes" :min="1" :max="1440" style="width:100%" />
                <div class="form-hint">锁定期间拒绝该 IP 登录</div>
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="IP 白名单（逗号分隔，留空 = 不限制）">
            <el-input v-model="sec.allowed_ips" type="textarea" :rows="2" placeholder="127.0.0.1, 192.168.1.100" />
            <div class="form-hint">仅允许列表内 IP 访问面板；留空则任何来源均可</div>
          </el-form-item>
          <div class="form-actions">
            <el-button :loading="saving.sec" @click="saveSecurity">保存安全策略</el-button>
            <el-button type="danger" plain :loading="saving.forceLogout" @click="forceLogoutAll">强制全部下线</el-button>
          </div>
        </el-form>
      </div>

      <!-- 登录日志 -->
      <div class="form-section">
        <div class="form-sub-title">登录日志（最近 {{ logs.length }} 条）</div>
        <el-button text type="primary" size="small" :loading="loadingLogs" @click="loadLogs">刷新</el-button>
        <el-table :data="logs" size="small" style="width:100%;margin-top:10px" max-height="320">
          <el-table-column prop="time" label="时间" width="170" />
          <el-table-column prop="ip" label="IP" width="140" />
          <el-table-column prop="username" label="用户名" min-width="100" />
          <el-table-column prop="result" label="结果" width="70">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : 'danger'" size="small" effect="plain">{{ row.result }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="备注" min-width="160" />
        </el-table>
      </div>

    </el-card>
  </div>
  `,

  data() {
    return {
      pages: [
        { id: 'dashboard', label: '看板' },
        { id: 'config', label: '配置' },
        { id: 'settings', label: '设置' },
        { id: 'plugins', label: '插件' },
        { id: 'market', label: '市场' },
        { id: 'console', label: '控制台' },
      ],
      prefs: { theme: 'dark', default_page: 'dashboard', dashboard_refresh: 10, bg_effects: true, density: 'standard' },
      cred: { username: '', old: '', new1: '', new2: '' },
      sec: { session_ttl_minutes: 0, allowed_ips: '', login_fail_max: 5, login_lock_minutes: 15 },
      logs: [],
      saving: { cred: false, sec: false, forceLogout: false },
      loadingLogs: false,
    };
  },

  created() {
    this.loadPrefs();
    this.loadSecurity();
    this.loadLogs();
    this.loadUser();
  },

  methods: {
    // ---------- 个性化 ----------
    loadPrefs() {
      try {
        const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
        this.prefs = Object.assign(this.prefs, raw);
      } catch (e) { /* 用默认值 */ }
      // 主题跟随现状（lunarx_theme），避免与顶栏切换不同步
      this.prefs.theme = localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark';
    },

    onThemeChange(theme) {
      // 主题由 app.js 的 applyTheme 统一处理，这里只需写入并派发事件
      localStorage.setItem(THEME_KEY, theme);
      window.dispatchEvent(new CustomEvent('lunarx-theme-change', { detail: theme }));
      window.setTheme && window.setTheme(theme);
    },

    savePrefs() {
      try {
        localStorage.setItem(PREFS_KEY, JSON.stringify(this.prefs));
        window.dispatchEvent(new CustomEvent('lunarx-prefs-change', { detail: this.prefs }));
        this.$message.success('个性化设置已保存');
      } catch (e) {
        this.$message.error('保存失败：' + e.message);
      }
    },

    // ---------- 账户 ----------
    async loadUser() {
      const data = await Api.tryGet('/api/auth_status', null);
      if (data && data.username) this.cred.username = data.username;
    },

    async saveCred() {
      // 用户名或密码任一项要改，都必须提供当前密码
      const username = this.cred.username.trim();
      if (!this.cred.old) {
        this.$message.warning('请填写当前密码');
        return;
      }
      if (this.cred.new1 !== this.cred.new2) {
        this.$message.warning('两次输入的新密码不一致');
        return;
      }
      this.saving.cred = true;
      try {
        const data = await Api.post('/api/change_password', {
          old_password: this.cred.old,
          new_password: this.cred.new1 || this.cred.old,   // 密码不改时原样提交
          new_username: username,
        });
        this.$message.success((data && data.message) || '凭据已保存');
        this.cred.old = this.cred.new1 = this.cred.new2 = '';
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.saving.cred = false;
      }
    },

    // ---------- 安全策略 ----------
    async loadSecurity() {
      try {
        const data = await Api.get('/api/webui_security');
        this.sec.session_ttl_minutes = data.session_ttl_minutes != null ? data.session_ttl_minutes : 0;
        this.sec.login_fail_max = data.login_fail_max != null ? data.login_fail_max : 5;
        this.sec.login_lock_minutes = data.login_lock_minutes != null ? data.login_lock_minutes : 15;
        this.sec.allowed_ips = Array.isArray(data.allowed_ips) ? data.allowed_ips.join(', ') : '';
      } catch (e) {
        this.$message.error('加载安全设置失败：' + e.message);
      }
    },

    async saveSecurity() {
      this.saving.sec = true;
      try {
        await Api.post('/api/webui_security', {
          session_ttl_minutes: this.sec.session_ttl_minutes,
          login_fail_max: this.sec.login_fail_max,
          login_lock_minutes: this.sec.login_lock_minutes,
          allowed_ips: this.sec.allowed_ips,
        });
        this.$message.success('安全策略已保存');
      } catch (e) {
        this.$message.error(e.message);
      } finally {
        this.saving.sec = false;
      }
    },

    async forceLogoutAll() {
      try {
        await this.$confirm('强制下线所有会话（含当前登录）？需要重新登录。', '强制下线', { type: 'warning' });
      } catch (e) {
        return;
      }
      this.saving.forceLogout = true;
      try {
        await Api.post('/api/webui_security/force_logout');
        this.$message.success('已强制全部下线，正在跳转登录页');
        setTimeout(() => { location.href = '/login'; }, 800);
      } catch (e) {
        this.$message.error(e.message);
        this.saving.forceLogout = false;
      }
    },

    // ---------- 登录日志 ----------
    async loadLogs() {
      this.loadingLogs = true;
      try {
        const data = await Api.get('/api/login_logs?limit=100');
        this.logs = (Array.isArray(data) ? data : []).map(line => {
          // 行格式：时间 | IP | 用户名 | 结果 | 备注
          const parts = String(line).split(' | ');
          return {
            time: parts[0] || '',
            ip: parts[1] || '',
            username: parts[2] || '',
            result: parts[3] || '',
            ok: parts[3] === '成功',
            reason: parts[4] || '',
          };
        });
      } catch (e) {
        this.logs = [];
        this.$message.error('加载登录日志失败：' + e.message);
      } finally {
        this.loadingLogs = false;
      }
    },
  },
};
