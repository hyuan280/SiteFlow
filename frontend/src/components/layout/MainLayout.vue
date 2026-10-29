<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";

import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();

const passwordDialog = ref(false);
const passwordFormRef = ref<FormInstance>();
const passwordSaving = ref(false);
const passwordForm = reactive({
  currentPassword: "",
  newPassword: "",
  confirmPassword: "",
});

const passwordRules: FormRules = {
  currentPassword: [{ required: true, message: "请输入当前密码", trigger: "blur" }],
  newPassword: [
    { required: true, message: "请输入新密码", trigger: "blur" },
    { min: 8, message: "新密码至少 8 位", trigger: "blur" },
  ],
  confirmPassword: [
    { required: true, message: "请再次输入新密码", trigger: "blur" },
    {
      validator: (_rule, value, callback) => {
        if (value !== passwordForm.newPassword) {
          callback(new Error("两次输入的新密码不一致"));
        } else {
          callback();
        }
      },
      trigger: "blur",
    },
  ],
};

const canChangePassword = computed(() => auth.user?.can_change_password ?? false);
const authSourceLabel = computed(() => {
  const source = auth.user?.auth_source;
  if (source === "env") return "环境变量";
  if (source === "db") return "数据库";
  if (source === "bootstrap") return "初始随机密码";
  return "未配置";
});

const menuItems = [
  {
    path: "/dashboard",
    label: "数据面板",
    paths: ["M3 3h7v9H3z", "M14 3h7v5h-7z", "M14 12h7v9h-7z", "M3 16h7v5H3z"],
  },
  {
    path: "/analytics",
    label: "运行分析",
    paths: ["M3 3v18h18", "M18 17V9", "M13 17V5", "M8 17v-3"],
  },
  {
    path: "/runs",
    label: "任务调度",
    paths: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 6v6l4 2"],
  },
  {
    path: "/browsers",
    label: "车道状态",
    paths: ["M2 3h20v14H2z", "M8 21h8", "M12 17v4"],
  },
  {
    path: "/features",
    label: "功能管理",
    paths: ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M14 14h7v7h-7z", "M3 14h7v7H3z"],
  },
  {
    path: "/templates",
    label: "模板管理",
    paths: ["M12 2 2 7l10 5 10-5z", "M2 17l10 5 10-5", "M2 12l10 5 10-5"],
  },
  {
    path: "/sites",
    label: "站点管理",
    paths: [
      "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
      "M2 12h20",
      "M12 2a15 15 0 0 1 0 20",
      "M12 2a15 15 0 0 0 0 20",
    ],
  },
  {
    path: "/notifications",
    label: "通知设置",
    paths: [
      "M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9",
      "M13.73 21a2 2 0 0 1-3.46 0",
    ],
  },
  {
    path: "/plugins",
    label: "插件管理",
    paths: [
      "M8 3h3v2a2 2 0 0 0 4 0V3h3a2 2 0 0 1 2 2v3h2a2 2 0 0 1 0 4h-2v3a2 2 0 0 1-2 2h-3v-2a2 2 0 0 0-4 0v2H8a2 2 0 0 1-2-2v-3H4a2 2 0 0 1 0-4h2V5a2 2 0 0 1 2-2z",
    ],
  },
  {
    path: "/settings",
    label: "系统设置",
    paths: [
      "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z",
      "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    ],
  },
];

async function handleCommand(command: string): Promise<void> {
  if (command === "password") {
    passwordForm.currentPassword = "";
    passwordForm.newPassword = "";
    passwordForm.confirmPassword = "";
    passwordDialog.value = true;
    return;
  }
  if (command === "logout") {
    try {
      await ElMessageBox.confirm("确认退出当前管理员会话？", "退出登录", {
        confirmButtonText: "退出",
        cancelButtonText: "取消",
        type: "warning",
      });
    } catch {
      return;
    }
    await auth.logout();
    ElMessage.success("已退出登录");
    await router.replace({ name: "login" });
  }
}

async function changePassword(): Promise<void> {
  if (!passwordFormRef.value) return;
  const valid = await passwordFormRef.value.validate().catch(() => false);
  if (!valid) return;
  passwordSaving.value = true;
  try {
    await auth.changePassword(passwordForm.currentPassword, passwordForm.newPassword);
    ElMessage.success("密码已保存到数据库");
    passwordDialog.value = false;
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "修改密码失败");
  } finally {
    passwordSaving.value = false;
  }
}
</script>

<template>
  <div class="app-shell">
    <header class="app-topbar">
      <router-link to="/dashboard" class="brand">
        <span class="brand-mark">SF</span>
        <span>
          <strong>SiteFlow</strong>
          <small>站点签到管理控制台</small>
        </span>
      </router-link>

      <el-dropdown trigger="hover" @command="handleCommand">
        <button type="button" class="account-trigger">
          <span class="avatar">{{ (auth.user?.username?.slice(0, 1) ?? "?").toUpperCase() }}</span>
          <span class="account-text">
            <strong>{{ auth.user?.username }}</strong>
            <small>{{ authSourceLabel }}</small>
          </span>
          <span class="caret">▾</span>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="password" :disabled="!canChangePassword">
              修改密码
            </el-dropdown-item>
            <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </header>

    <div class="app-body">
      <aside class="app-sidebar">
        <el-menu :default-active="route.path" router class="side-menu">
          <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
            <svg
              class="menu-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path v-for="(path, index) in item.paths" :key="index" :d="path" />
            </svg>
            <span>{{ item.label }}</span>
          </el-menu-item>
        </el-menu>
      </aside>
      <section class="app-content">
        <router-view />
      </section>
    </div>

    <el-dialog v-model="passwordDialog" title="修改管理员密码" width="min(440px, 92vw)">
      <el-form
        ref="passwordFormRef"
        :model="passwordForm"
        :rules="passwordRules"
        label-position="top"
      >
        <el-form-item label="当前密码" prop="currentPassword">
          <el-input v-model="passwordForm.currentPassword" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码" prop="newPassword">
          <el-input v-model="passwordForm.newPassword" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认新密码" prop="confirmPassword">
          <el-input
            v-model="passwordForm.confirmPassword"
            type="password"
            show-password
            @keyup.enter="changePassword"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button type="primary" :loading="passwordSaving" @click="changePassword">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.app-shell {
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.app-topbar {
  height: 64px;
  flex: 0 0 64px;
  padding: 0 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  background: rgba(255, 255, 255, 0.9);
  border-bottom: 1px solid #e4e9f0;
  position: relative;
  z-index: 20;
  backdrop-filter: blur(14px);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  color: inherit;
  text-decoration: none;
}

.brand-mark {
  width: 38px;
  height: 38px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #409eff, #2f7ed8);
  color: #fff;
  font-weight: 700;
  font-size: 13px;
}

.brand strong {
  display: block;
  font-size: 17px;
}

.brand small {
  display: block;
  margin-top: 1px;
  color: #8a96a3;
  font-size: 11px;
}

.account-trigger {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.account-trigger:hover {
  border-color: #dbe4ef;
  background: #f8fafc;
}

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: #eaf3ff;
  color: #2f7ed8;
  font-weight: 700;
}

.account-text {
  display: flex;
  flex-direction: column;
  text-align: left;
}

.account-text strong {
  font-size: 13px;
}

.account-text small {
  color: #8a96a3;
  font-size: 11px;
}

.caret {
  color: #8a96a3;
  font-size: 12px;
}

.app-body {
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
}

.app-sidebar {
  width: 210px;
  flex: 0 0 210px;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 18px 12px;
  border-right: 1px solid #e4e9f0;
  background: rgba(255, 255, 255, 0.6);
}

.menu-icon {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  margin-right: 9px;
}

.side-menu {
  border-right: none;
  background: transparent;
}

.side-menu :deep(.el-menu-item) {
  height: 42px;
  line-height: 42px;
  margin-bottom: 4px;
  border-radius: 10px;
  color: #4b5b6b;
}

.side-menu :deep(.el-menu-item.is-active) {
  background: #eaf3ff;
  color: #2f7ed8;
  font-weight: 600;
}

.app-content {
  flex: 1;
  min-width: 0;
  min-height: 0;
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 26px 30px 48px;
}

@media (max-width: 800px) {
  .app-topbar {
    padding: 0 16px;
  }

  .brand small {
    display: none;
  }

  .app-body {
    flex-direction: column;
  }

  .app-sidebar {
    width: 100%;
    height: auto;
    flex: none;
    padding: 10px 12px;
    border-right: none;
    border-bottom: 1px solid #e4e9f0;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .menu-icon {
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  margin-right: 9px;
}

.side-menu {
    display: flex;
    gap: 4px;
  }

  .side-menu :deep(.el-menu-item) {
    margin-bottom: 0;
    white-space: nowrap;
  }

  .app-content {
    height: auto;
    padding: 18px 16px 36px;
  }
}
</style>
