<script lang="ts">
  import { onMount } from 'svelte';
  import type { ECharts } from 'echarts/core';
  import { toEChartsOption } from '#lib/chart';
  import type { DatasetRow, VisualizationSpec } from '#lib/types';

  export let spec: VisualizationSpec;
  export let rows: DatasetRow[];

  let host: HTMLDivElement;
  let chart: ECharts | undefined;
  let resizeFrame: number | undefined;

  function render() {
    if (chart) chart.setOption(toEChartsOption(spec, rows), true);
  }

  function resizeChart() {
    if (resizeFrame !== undefined) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = undefined;
      if (!chart || !host.isConnected) return;
      const width = Math.floor(host.getBoundingClientRect().width);
      if (width > 0) chart.resize({ width });
    });
  }

  $: if (chart && spec && rows) render();

  onMount(() => {
    let resize: ResizeObserver | undefined;
    let disposed = false;
    void import('echarts').then((echarts) => {
      if (disposed) return;
      chart = echarts.init(host);
      render();
      resize = new ResizeObserver(resizeChart);
      resize.observe(host);
      if (host.parentElement) resize.observe(host.parentElement);
      window.addEventListener('resize', resizeChart);
      resizeChart();
    });
    return () => {
      disposed = true;
      window.removeEventListener('resize', resizeChart);
      if (resizeFrame !== undefined) cancelAnimationFrame(resizeFrame);
      resize?.disconnect();
      chart?.dispose();
    };
  });
</script>

<div class="chart" bind:this={host} role="img" aria-label={spec.title}></div>

<style>
  .chart {
    contain: layout size;
    max-width: 100%;
    min-height: 300px;
    min-width: 0;
    overflow: hidden;
    width: 100%;
  }

  .chart :global(> div),
  .chart :global(canvas),
  .chart :global(svg) {
    max-width: 100% !important;
  }
</style>
