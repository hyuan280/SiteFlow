<script setup lang="ts">
defineProps<{
  eyebrow: string;
  title: string;
  description?: string;
}>();
</script>

<template>
  <header class="page-header">
    <div v-if="$slots.leading" class="page-header__leading">
      <slot name="leading" />
    </div>
    <p class="page-header__eyebrow">{{ eyebrow }}</p>
    <div class="page-header__row">
      <h1 class="page-header__title">{{ title }}</h1>
      <div v-if="$slots.actions" class="page-header__actions">
        <slot name="actions" />
      </div>
    </div>
    <p v-if="description" class="page-header__description">{{ description }}</p>
  </header>
</template>

<style scoped>
.page-header {
  flex: none;
  min-width: 0;
  margin-bottom: 20px;
}

.page-header__leading {
  margin-bottom: 6px;
}

.page-header__eyebrow {
  margin: 0 0 4px;
  color: #409eff;
  font-size: 11px;
  font-weight: 700;
  line-height: 16px;
  letter-spacing: 0.16em;
}

.page-header__row {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 16px;
  height: 36px;
  min-width: 0;
}

.page-header__title {
  flex: 0 0 auto;
  max-width: 60%;
  margin: 0;
  font-size: 26px;
  line-height: 36px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.page-header__title:only-child {
  max-width: 100%;
}

.page-header__actions {
  display: flex;
  flex: 0 1 auto;
  flex-wrap: nowrap;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  min-width: 0;
  margin-left: auto;
  overflow-x: auto;
  scrollbar-width: none;
}

.page-header__actions::-webkit-scrollbar {
  display: none;
}

.page-header__actions :deep(> *) {
  flex-shrink: 0;
}

.page-header__actions :deep(button),
.page-header__leading :deep(button) {
  height: 32px;
  max-height: 32px;
  margin: 0;
  flex-shrink: 0;
}

.page-header__description {
  margin: 4px 0 0;
  color: #7a8794;
  font-size: 14px;
  line-height: 22px;
  overflow-wrap: anywhere;
}

@media (max-width: 600px) {
  .page-header__title {
    font-size: 22px;
  }
}
</style>
