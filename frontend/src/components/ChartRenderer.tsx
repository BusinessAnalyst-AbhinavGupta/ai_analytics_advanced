"use client";

import React from 'react';
import {
  LineChart, Line, BarChart, Bar, AreaChart, Area, ScatterChart, Scatter,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer
} from 'recharts';

export interface ChartConfig {
  type: "LineChart" | "BarChart" | "AreaChart" | "ScatterChart";
  xKey: string;
  series: {
    key: string;
    name?: string;
    color?: string;
  }[];
}

interface ChartRendererProps {
  data: any[];
  config: ChartConfig;
  height?: number;
}

const DEFAULT_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

export function ChartRenderer({ data, config, height = 400 }: ChartRendererProps) {
  if (!data || data.length === 0) return <div>No data available</div>;

  const renderTooltip = () => (
    <Tooltip
      contentStyle={{
        backgroundColor: 'rgba(26, 29, 36, 0.9)',
        border: '1px solid rgba(255,255,255,0.1)',
        borderRadius: '8px',
        color: '#f0f2f5'
      }}
      itemStyle={{ color: '#f0f2f5' }}
    />
  );

  const renderSeries = () => {
    return config.series.map((s, idx) => {
      const color = s.color || DEFAULT_COLORS[idx % DEFAULT_COLORS.length];
      
      switch (config.type) {
        case "LineChart":
          return <Line key={s.key} type="monotone" dataKey={s.key} name={s.name || s.key} stroke={color} strokeWidth={2} dot={{ r: 4 }} activeDot={{ r: 6 }} />;
        case "BarChart":
          return <Bar key={s.key} dataKey={s.key} name={s.name || s.key} fill={color} radius={[4, 4, 0, 0]} />;
        case "AreaChart":
          return <Area key={s.key} type="monotone" dataKey={s.key} name={s.name || s.key} stroke={color} fill={color} fillOpacity={0.3} />;
        case "ScatterChart":
          return <Scatter key={s.key} name={s.name || s.key} dataKey={s.key} fill={color} />;
        default:
          return null;
      }
    });
  };

  const renderChart = () => {
    const commonProps = {
      data,
      margin: { top: 20, right: 30, left: 20, bottom: 20 }
    };

    switch (config.type) {
      case "LineChart":
        return (
          <LineChart {...commonProps}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
            <XAxis dataKey={config.xKey} stroke="#9ba1a6" />
            <YAxis stroke="#9ba1a6" />
            {renderTooltip()}
            <Legend />
            {renderSeries()}
          </LineChart>
        );
      case "BarChart":
        return (
          <BarChart {...commonProps}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
            <XAxis dataKey={config.xKey} stroke="#9ba1a6" />
            <YAxis stroke="#9ba1a6" />
            {renderTooltip()}
            <Legend />
            {renderSeries()}
          </BarChart>
        );
      case "AreaChart":
        return (
          <AreaChart {...commonProps}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
            <XAxis dataKey={config.xKey} stroke="#9ba1a6" />
            <YAxis stroke="#9ba1a6" />
            {renderTooltip()}
            <Legend />
            {renderSeries()}
          </AreaChart>
        );
      case "ScatterChart":
        return (
          <ScatterChart {...commonProps}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
            <XAxis type="category" dataKey={config.xKey} name={config.xKey} stroke="#9ba1a6" />
            {config.series.map((s, idx) => (
              <YAxis key={`y-${s.key}`} yAxisId={idx} stroke="#9ba1a6" />
            ))}
            {renderTooltip()}
            <Legend />
            {renderSeries()}
          </ScatterChart>
        );
      default:
        return <div>Unsupported chart type: {config.type}</div>;
    }
  };

  return (
    <div style={{ width: '100%', height }} className="animate-fade-in">
      <ResponsiveContainer>
        {renderChart()}
      </ResponsiveContainer>
    </div>
  );
}
