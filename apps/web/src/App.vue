<script setup lang="ts">
const appName: string = "TradeOS";
</script>

<template>
  <main :aria-label="appName">
    <RouterView />
  </main>
</template>
