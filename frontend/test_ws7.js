const puppeteer = require('puppeteer');

(async () => {
  const browser = await puppeteer.launch();
  const page = await browser.newPage();
  
  // Set up console listener to print to terminal
  page.on('console', msg => console.log('PAGE LOG:', msg.text()));

  await page.goto('http://localhost:3000/junior');
  
  // Wait a few seconds for WS to connect
  await new Promise(r => setTimeout(r, 5000));
  
  await browser.close();
})();
