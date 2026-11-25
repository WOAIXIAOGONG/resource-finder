const statusNode = document.querySelector('#status');
async function update(message = {action: 'status'}) {
  try {
    const state = await chrome.runtime.sendMessage(message);
    statusNode.textContent = state.message;
  } catch (error) { statusNode.textContent = '连接失败：' + error.message; }
}
document.querySelector('#connect').addEventListener('click', async () => {
  await update({action: 'connect', address: document.querySelector('#pair').value.trim()});
  document.querySelector('#pair').value = '';
});
document.querySelector('#disconnect').addEventListener('click', () => update({action: 'disconnect'}));
const aigeiOrigins = ['https://www.aigei.com/*', 'https://aigei.com/*'];
const aigeiStatus = document.querySelector('#aigei-status');
document.querySelector('#aigei').addEventListener('click', async () => {
  try {
    const granted = await chrome.permissions.request({origins:aigeiOrigins});
    aigeiStatus.textContent = granted ? '已启用：请在目标资源网页播放一次。' : '未启用；其他搜索功能可继续使用。';
  } catch (error) { aigeiStatus.textContent = error.message; }
});
chrome.permissions.contains({origins:aigeiOrigins}).then(granted => {
  if (granted) aigeiStatus.textContent = '已启用爱给播放提取';
});
update();
setInterval(() => update(), 1500);
