const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  
  // Navigate to an empty page so we can run JS
  await page.goto('about:blank');
  
  // Set up console listener to print to terminal
  page.on('console', msg => console.log('PAGE LOG:', msg.text()));

  await page.evaluate(() => {
    return new Promise((resolve) => {
      console.log('Connecting to WS...');
      const ws = new WebSocket('ws://localhost:8000/ws/tenants/1/activity');
      ws.onopen = () => {
        console.log('WS OPENED SUCCESSFULLY!');
        ws.close();
        resolve();
      };
      ws.onerror = (e) => {
        console.log('WS ERROR!');
        resolve();
      };
      ws.onclose = () => {
        console.log('WS CLOSED!');
        resolve();
      };
    });
  });
  
  await browser.close();
})();
