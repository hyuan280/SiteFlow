<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiRequest } from "@/api/client";

const props = defineProps<{ modelValue: string; capability: "secret.credentials" | "secret.totp" }>();
const emit = defineEmits<{ (event: "update:modelValue", value: string): void }>();
const providers = ref<{ id: string; name: string; capability: string }[]>([]);
const error = ref("");
onMounted(async () => {
  try {
    providers.value = (await apiRequest<typeof providers.value>("/secret-providers"))
      .filter(item => item.capability === props.capability);
  } catch {
    error.value = "Provider catalog unavailable";
  }
});
</script>

<template>
  <select :value="modelValue" aria-label="Secret provider" @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)">
    <option v-for="provider in providers" :key="provider.id" :value="provider.id">{{ provider.name }}</option>
    <option v-if="!providers.some(item => item.id === modelValue)" :value="modelValue">{{ modelValue }} (current / unavailable)</option>
  </select>
  <small v-if="error" role="alert">{{ error }}</small>
</template>
