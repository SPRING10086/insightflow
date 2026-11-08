# Third-party notices

## Apache ECharts

This project bundles Apache ECharts for offline chart rendering.

- Project: https://echarts.apache.org/
- Source: https://github.com/apache/echarts
- License: Apache License 2.0
- Bundled file: `src/insightflow/assets/echarts.min.js`
- Copyright: Apache Software Foundation and ECharts contributors

The bundled file is used unmodified except for embedding it into the generated
single-file HTML report. No network request is made at report runtime.

Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0

## Python dependencies

The project depends on pandas, numpy, openpyxl and xlrd. Their licenses are
available in the respective distributions. They are not bundled into this
repository.
