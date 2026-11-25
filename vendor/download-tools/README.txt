便携下载组件

本目录必须与资源搜寻器一起完整保留。用户无需安装这些组件。
yt-dlp负责解析及下载，Deno运行随yt-dlp提供的YouTube脚本组件，
FFmpeg负责合并、音轨提取与格式转换，ffprobe检查输出轨道。

确切版本、发布地址、SHA256见manifest.json；各组件许可文件随本目录提供。
组件独立进程运行，本工具未修改这些二进制。开发者更新后必须重新实测，
不要以可执行文件存在代替平台真实下载通过。

项目与源码：
https://github.com/yt-dlp/yt-dlp
https://github.com/denoland/deno
https://ffmpeg.org/download.html
FFmpeg所用构建与配置/源码说明：https://www.gyan.dev/ffmpeg/builds/

仅下载你有权保存的资源；登录、付费、DRM或站点风控限制不会被绕过。
