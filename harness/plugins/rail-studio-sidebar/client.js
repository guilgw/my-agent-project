window.__ModuleLoader__.load({
  id: '@local/rail-studio-sidebar',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    // 与启动脚本的默认端口一致。改了 RAIL_STUDIO_PORT 后，请同步改这里，或在地址栏手动打开。
    const DEFAULT_STUDIO_URL = 'http://127.0.0.1:18770';

    return {
      inject: ['slots', 'sidebarRight', 'locale'],
      apply(ctx, config) {
        const configured = config && typeof config.studioUrl === 'string' ? config.studioUrl.trim() : '';
        const url = configured || DEFAULT_STUDIO_URL;
        ctx.effect(() => ctx.locale.register('railStudioSidebar', {
          zh: {
            open: '打开工作台',
            hint: '在右侧浏览器打开轨道工作台',
          },
          en: {
            open: 'Open studio',
            hint: 'Open the rail studio in the browser sidebar',
          },
        }));
        const t = ctx.locale.bind('railStudioSidebar');
        const storageKey = 'rail-studio-sidebar-opened';

        function openStudio() {
          ctx.sidebarRight.openTab('browser', { params: { url } });
        }

        function StudioDock() {
          const [detail, setDetail] = React.useState('');
          React.useEffect(() => {
            let seen = '';
            try { seen = sessionStorage.getItem(storageKey) || ''; } catch (error) { seen = ''; }
            if (seen === url) return undefined;
            try {
              openStudio();
              try { sessionStorage.setItem(storageKey, url); } catch (error) { /* 隐私模式不影响按钮 */ }
            } catch (error) {
              setDetail(error && error.message ? error.message : String(error));
            }
            return undefined;
          }, []);
          return h('button', {
            type: 'button',
            onClick() {
              try { openStudio(); setDetail(''); }
              catch (error) { setDetail(error && error.message ? error.message : String(error)); }
            },
            title: detail || t('hint'),
            style: {
              margin: '0 8px 0 0',
              padding: '4px 10px',
              borderRadius: '999px',
              border: '1px solid currentColor',
              background: 'transparent',
              color: 'inherit',
              cursor: 'pointer',
              font: 'inherit',
            },
          }, t('open'));
        }

        ctx.slots.inject('conversation.composer.dock', () => ctx.slots.register({
          name: 'conversation.composer.dock',
          id: 'rail-studio-sidebar',
          order: 20,
        }, StudioDock));
      },
    };
  },
});
