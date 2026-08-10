/* ============================================================
 * Lunar X 控制面板 — 应用入口
 *
 * 结构：Navigation Rail（84px）+ 顶栏 + 页面容器
 * 路由：手写 hash 路由，形如 #/dashboard、#/settings/bot
 * 主题：dark / light 两档，存 localStorage.lunarx_theme
 * ============================================================ */
(function () {
  'use strict';

  const THEME_KEY = 'lunarx_theme';

  /**
   * 读取主题，并兼容旧版三主题的取值。
   * 旧值 pink / blue → dark，white → light。
   */
  function readTheme() {
    const raw = localStorage.getItem(THEME_KEY);
    if (raw === 'dark' || raw === 'light') return raw;
    if (raw === 'white') return 'light';
    return 'dark';                     // pink / blue / 空值都归到深色
  }

  function applyTheme(theme) {
    document.body.dataset.theme = theme;
    // Element Plus 的深色变量靠 html.dark 生效
    document.documentElement.classList.toggle('dark', theme === 'dark');
    localStorage.setItem(THEME_KEY, theme);
  }

  // ---------- 格式化工具 ----------
  // 必须注册到 app.config.globalProperties：Vue 3 模板表达式解析不到 window
  // 上的全局函数（渲染代理的 has 拦截阻止了全局回退）
  function fmtBytes(b) {
    if (b === null || b === undefined || isNaN(b)) return '-';
    if (b < 1024) return b + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = b / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 2 : 1) + ' ' + units[i];
  }

  function fmtDuration(s) {
    if (s === null || s === undefined || isNaN(s)) return '-';
    s = Math.floor(s);
    if (s < 60) return s + '秒';
    if (s < 3600) return Math.floor(s / 60) + '分钟';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h + '小时' + m + '分';
  }

  function fmtTime(t) {
    if (!t) return '-';
    const d = new Date(t * 1000);
    const p = n => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
           ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  // ---------- 导航定义 ----------
  const MENU = [
    { id: 'dashboard', label: '看板',   icon: 'Odometer', title: '数据看板', sub: '框架、协议端与服务器的实时运行状态（每 10 秒自动刷新）' },
    { id: 'settings',  label: '设置',   icon: 'Setting',  title: '设置',     sub: '协议端连接、框架参数、AI 人格与管理员权限' },
    { id: 'plugins',   label: '插件',   icon: 'Box',      title: '插件管理', sub: '已安装插件的启用、帮助与卸载' },
    { id: 'market',    label: '市场',   icon: 'Shop',     title: '插件市场', sub: '插件源设置与在线插件安装' },
    { id: 'console',   label: '控制台', icon: 'Monitor',  title: '控制台',   sub: 'Lunar X 框架实时运行日志（长轮询，约 50ms 延迟）' },
  ];

  // 旧版书签兼容：#/protocol 这类地址重定向到现在的位置。
  // persona 的配置已经搬进 ai_chat 插件，所以指到插件管理页。
  const LEGACY_ROUTES = {
    protocol: 'settings/protocol',
    bot: 'settings/bot',
    users: 'settings/users',
    persona: 'plugins',
  };

  const App = {
    template: `
    <div class="app-shell">
      <nav class="rail">
        <div v-for="item in menu" :key="item.id"
             class="rail-item" :class="{ active: page === item.id }"
             :title="item.title" @click="go(item.id)">
          <div class="rail-icon"><el-icon><component :is="item.icon" /></el-icon></div>
          <div class="rail-label">{{ item.label }}</div>
        </div>

        <div class="rail-spacer"></div>

        <div class="rail-btn" :title="theme === 'dark' ? '切换到浅色' : '切换到深色'" @click="toggleTheme">
          <el-icon><component :is="theme === 'dark' ? 'Sunny' : 'Moon'" /></el-icon>
        </div>

        <el-dropdown trigger="click" placement="right-end" @command="onUserCommand">
          <div class="rail-avatar" :title="username || '未登录'">{{ avatarText }}</div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item disabled>{{ username || '未登录' }}</el-dropdown-item>
              <el-dropdown-item command="password" divided>修改密码</el-dropdown-item>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </nav>

      <div class="main-area">
        <header class="topbar">
          <div>
            <div class="topbar-title">{{ current.title }}</div>
            <div class="topbar-sub">{{ current.sub }}</div>
          </div>
          <div class="topbar-spacer"></div>
          <span class="topbar-ver">v{{ version }}</span>
          <el-button size="small" :loading="restarting" @click="doRestart">
            <el-icon style="margin-right:4px"><RefreshRight /></el-icon>重启机器人
          </el-button>
        </header>

        <main class="page-body">
          <component :is="currentComp" :key="page" :sub-route="subRoute" />
        </main>
      </div>

      <el-dialog v-model="passwordVisible" :title="securityMode ? '安全提醒' : '修改密码'"
                 width="420px" append-to-body
                 :close-on-click-modal="!securityMode" :close-on-press-escape="!securityMode"
                 :show-close="!securityMode">
        <div v-if="securityMode" class="sec-banner">
          <span class="sec-banner-dot"></span>
          <div>
            <div class="sec-banner-title">检测到默认凭据</div>
            <div class="sec-banner-msg">当前仍在使用出厂默认的用户名和密码（lunarx / lunarx），控制台存在被未授权访问的风险。请立即设置新的用户名和密码后再使用。</div>
          </div>
        </div>
        <el-form label-position="top" @submit.prevent>
          <el-form-item v-if="securityMode" label="新用户名">
            <el-input v-model="pw.user" placeholder="自定义用户名" maxlength="32" />
          </el-form-item>
          <el-form-item v-if="!securityMode" label="旧密码">
            <el-input v-model="pw.old" type="password" show-password autocomplete="current-password" />
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="pw.new1" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="pw.new2" type="password" show-password autocomplete="new-password"
                      @keyup.enter="doChangePassword" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button v-if="!securityMode" @click="passwordVisible = false">取消</el-button>
          <el-button type="primary" :loading="pwSaving" @click="doChangePassword">确认修改</el-button>
        </template>
      </el-dialog>
    </div>
    `,

    data() {
      return {
        menu: MENU,
        page: 'dashboard',
        subRoute: '',
        theme: readTheme(),
        version: '0.0.0',
        username: '',
        restarting: false,
        passwordVisible: false,
        securityMode: false,
        pwSaving: false,
        pw: { user: '', old: '', new1: '', new2: '' },
      };
    },

    computed: {
      current() {
        return this.menu.find(m => m.id === this.page) || this.menu[0];
      },
      currentComp() {
        const key = this.page.charAt(0).toUpperCase() + this.page.slice(1);
        return window.Pages[key];
      },
      avatarText() {
        return this.username ? this.username.charAt(0).toUpperCase() : '?';
      },
    },

    async created() {
      applyTheme(this.theme);
      window.addEventListener('hashchange', this.onHash);
      this.onHash();
      await this.loadUser();
      this.loadVersion();
      this.checkDefaultCredentials();
    },

    beforeUnmount() {
      window.removeEventListener('hashchange', this.onHash);
    },

    methods: {
      /** 解析 #/page 或 #/page/sub，非法路由回落到看板 */
      onHash() {
        const raw = (location.hash || '#/dashboard').replace(/^#\/?/, '');
        const parts = raw.split('/').filter(Boolean);
        let id = parts[0] || 'dashboard';

        if (LEGACY_ROUTES[id]) {
          location.hash = '#/' + LEGACY_ROUTES[id];
          return;                       // hashchange 会再次触发本函数
        }
        if (!this.menu.some(m => m.id === id)) id = 'dashboard';

        this.page = id;
        this.subRoute = parts[1] || '';
      },

      go(id) {
        if (this.page === id) return;
        location.hash = '#/' + id;
      },

      toggleTheme() {
        this.theme = this.theme === 'dark' ? 'light' : 'dark';
        applyTheme(this.theme);
        // 通知页面（图表等）跟着重绘，否则要等下一次轮询才变色
        window.dispatchEvent(new CustomEvent('lunarx-theme-change', { detail: this.theme }));
      },

      async loadUser() {
        const data = await Api.tryGet('/api/auth_status', null);
        if (data) this.username = data.username || '';
      },

      async loadVersion() {
        const data = await Api.tryGet('/api/version', null);
        if (data && data.version) this.version = data.version;
      },

      onUserCommand(cmd) {
        if (cmd === 'password') {
          this.securityMode = false;
          this.passwordVisible = true;
        } else if (cmd === 'logout') {
          this.doLogout();
        }
      },

      /** 登录后检测是否仍为默认凭据；若是，弹出不可关闭的安全提醒要求立即改密 */
      async checkDefaultCredentials() {
        const data = await Api.tryGet('/api/check_default_credentials', null);
        if (data && data.is_default) {
          this.securityMode = true;
          this.passwordVisible = true;
        }
      },

      async doRestart() {
        try {
          await this.$confirm('确定重启机器人进程？连接会短暂中断。', '重启确认', { type: 'warning' });
        } catch (e) {
          return;                       // 用户点了取消
        }
        this.restarting = true;
        try {
          const data = await Api.post('/api/restart_bot');
          this.$message.success((data && data.message) || '重启中');
        } catch (e) {
          this.$message.error(e.message);
        } finally {
          this.restarting = false;
        }
      },

      async doLogout() {
        try { await Api.post('/api/logout'); } catch (e) { /* 无论成败都跳登录 */ }
        location.href = '/login';
      },

      async doChangePassword() {
        const old = this.securityMode ? 'lunarx' : this.pw.old;
        if (!old || !this.pw.new1) {
          this.$message.warning('请填写旧密码和新密码');
          return;
        }
        if (this.pw.new1 !== this.pw.new2) {
          this.$message.warning('两次输入的新密码不一致');
          return;
        }
        if (this.securityMode && !this.pw.user.trim()) {
          this.$message.warning('请设置新的用户名');
          return;
        }
        this.pwSaving = true;
        try {
          const data = await Api.post('/api/change_password', {
            old_password: old,
            new_password: this.pw.new1,
            new_username: this.securityMode ? this.pw.user.trim() : undefined,
          });
          this.$message.success((data && data.message) || '凭据已修改');
          this.username = this.securityMode ? this.pw.user.trim() : this.username;
          this.passwordVisible = false;
          this.securityMode = false;
          this.pw = { user: '', old: '', new1: '', new2: '' };
        } catch (e) {
          this.$message.error(e.message);
        } finally {
          this.pwSaving = false;
        }
      },
    },
  };

  async function boot() {
    // 登录守卫：未登录直接跳登录页，避免后续接口全部 401
    try {
      const status = await Api.get('/api/auth_status');
      if (!status || !status.logged_in) { location.href = '/login'; return; }
    } catch (e) {
      location.href = '/login';
      return;
    }

    applyTheme(readTheme());

    const app = Vue.createApp(App);
    app.use(ElementPlus);

    app.config.globalProperties.fmtBytes = fmtBytes;
    app.config.globalProperties.fmtDuration = fmtDuration;
    app.config.globalProperties.fmtTime = fmtTime;

    for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
      app.component(name, comp);
    }
    app.mount('#app');
  }

  window.fmtBytes = fmtBytes;
  window.fmtDuration = fmtDuration;
  window.fmtTime = fmtTime;
  window.setTheme = applyTheme;

  boot();
})();
