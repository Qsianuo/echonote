package com.echonote.app;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // 原生识别插件的唯一注册点；必须在 super.onCreate 之前。
        // （上次注册缺失的教训：写入命令链被失败的 cp 短路，构建照常成功，
        // 运行时报“插件不可用”——这次写完必须回读验证。）
        registerPlugin(SherpaASRPlugin.class);
        registerPlugin(SherpaMTPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
