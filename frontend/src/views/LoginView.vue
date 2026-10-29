<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, type FormInstance, type FormRules } from "element-plus";

import { ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const formRef = ref<FormInstance>();
const loading = ref(false);
const errorMessage = ref("");
const form = reactive({
  username: "admin",
  password: "",
});

const rules: FormRules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }],
};

async function submit(): Promise<void> {
  if (!formRef.value) return;
  const valid = await formRef.value.validate().catch(() => false);
  if (!valid) return;

  loading.value = true;
  errorMessage.value = "";
  try {
    await auth.login(form.username.trim(), form.password);
    ElMessage.success("登录成功");
    const redirect = typeof route.query.redirect === "string" ? route.query.redirect : "/dashboard";
    await router.replace(redirect);
  } catch (error) {
    if (error instanceof ApiError) {
      errorMessage.value = error.message;
    } else {
      errorMessage.value = "无法连接服务，请稍后再试";
    }
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-card">
      <div class="brand">
        <div class="brand-mark">SF</div>
        <div>
          <h1>SiteFlow</h1>
          <p>站点签到管理控制台</p>
        </div>
      </div>

      <el-alert
        v-if="errorMessage"
        :title="errorMessage"
        type="error"
        :closable="false"
        show-icon
        class="login-alert"
      />

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" placeholder="admin" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            placeholder="请输入管理员密码"
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" class="login-button" :loading="loading" @click="submit">
          登录
        </el-button>
      </el-form>

      <p class="hint">
        首次启动如果未配置管理员环境变量，随机密码会打印在 API 容器启动日志中；修改密码后会保存到数据库。
      </p>
    </section>
  </main>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 32px 20px;
}

.login-card {
  width: min(420px, 100%);
  padding: 34px;
  border: 1px solid #e4e9f0;
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 24px 70px rgba(31, 45, 61, 0.12);
  backdrop-filter: blur(16px);
}

.brand {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 28px;
}

.brand-mark {
  width: 48px;
  height: 48px;
  border-radius: 14px;
  display: grid;
  place-items: center;
  background: linear-gradient(135deg, #409eff, #2f7ed8);
  color: #fff;
  font-weight: 700;
  letter-spacing: 0.04em;
}

.brand h1 {
  margin: 0;
  font-size: 24px;
  line-height: 1.2;
}

.brand p {
  margin: 4px 0 0;
  color: #7a8794;
  font-size: 13px;
}

.login-alert {
  margin-bottom: 18px;
}

.login-button {
  width: 100%;
  margin-top: 4px;
}

.hint {
  margin: 22px 0 0;
  color: #8a96a3;
  font-size: 12px;
  line-height: 1.7;
}
</style>
